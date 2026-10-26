"""Create the PostgreSQL database and apply `db/schema.sql`.

Usage (from the project root):

    python db/init_db.py            # create database + tables if missing
    python db/init_db.py --reset    # DROP the businesses table and recreate it
    python db/init_db.py --reset --yes   # ... without the confirmation prompt
    python db/init_db.py --sql-only      # only apply schema to existing database

The connection string is read from DATABASE_URL (see .env / .env.example).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Make `app` importable when the script is run directly: python db/init_db.py
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

from app.config import settings  # noqa: E402

SCHEMA_FILE = Path(__file__).resolve().parent / "schema.sql"


def _quote_identifier(name: str) -> str:
    if not name:
        raise SystemExit("DATABASE_URL does not contain a database name.")
    return '"' + name.replace('"', '""') + '"'


def _split_statements(sql: str) -> list[str]:
    """Split a .sql file into executable statements.

    Semicolons inside string literals, quoted identifiers, comments and
    dollar-quoted bodies (``$$ ... $$`` / ``$tag$ ... $tag$``) do not end a
    statement, so function definitions such as the ``updated_at`` trigger
    survive intact.
    """
    statements: list[str] = []
    buffer: list[str] = []
    index = 0
    length = len(sql)

    in_single = in_double = False
    in_line_comment = in_block_comment = False
    dollar_tag: str | None = None

    while index < length:
        char = sql[index]
        nxt = sql[index + 1] if index + 1 < length else ""

        if in_line_comment:
            if char == "\n":
                in_line_comment = False
                buffer.append(char)
            index += 1
            continue

        if in_block_comment:
            if char == "*" and nxt == "/":
                in_block_comment = False
                buffer.append(" ")
                index += 2
            else:
                index += 1
            continue

        if dollar_tag is not None:
            if sql.startswith(dollar_tag, index):
                buffer.append(dollar_tag)
                index += len(dollar_tag)
                dollar_tag = None
            else:
                buffer.append(char)
                index += 1
            continue

        if in_single:
            buffer.append(char)
            if char == "'":
                if nxt == "'":
                    buffer.append(nxt)
                    index += 2
                    continue
                in_single = False
            index += 1
            continue

        if in_double:
            buffer.append(char)
            if char == '"':
                if nxt == '"':
                    buffer.append(nxt)
                    index += 2
                    continue
                in_double = False
            index += 1
            continue

        if char == "-" and nxt == "-":
            in_line_comment = True
            index += 2
            continue

        if char == "/" and nxt == "*":
            in_block_comment = True
            index += 2
            continue

        if char == "'":
            in_single = True
            buffer.append(char)
            index += 1
            continue

        if char == '"':
            in_double = True
            buffer.append(char)
            index += 1
            continue

        if char == "$":
            match = re.match(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$", sql[index:])
            if match:
                dollar_tag = match.group(0)
                buffer.append(dollar_tag)
                index += len(dollar_tag)
                continue

        if char == ";":
            statement = "".join(buffer).strip()
            if statement:
                statements.append(statement)
            buffer = []
            index += 1
            continue

        buffer.append(char)
        index += 1

    tail = "".join(buffer).strip()
    if tail:
        statements.append(tail)

    return statements


def ensure_database() -> str:
    """Create the target database if it does not exist. Returns its name."""
    url = make_url(settings.database_url)
    database = url.database
    if not database:
        raise SystemExit("DATABASE_URL must include a database name.")

    admin_url = url.set(database="postgres")
    try:
        admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        with admin_engine.connect() as connection:
            exists = connection.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": database}
            ).scalar()
            if exists:
                print(f"  database '{database}' already exists")
            else:
                connection.execute(text(f"CREATE DATABASE {_quote_identifier(database)}"))
                print(f"  created database '{database}'")
        admin_engine.dispose()
    except OperationalError as exc:
        print("\nCould not connect to PostgreSQL.", file=sys.stderr)
        print(f"  URL: {url.render_as_string(hide_password=True)}", file=sys.stderr)
        print(f"  Error: {str(exc.orig).strip()}", file=sys.stderr)
        print(
            "\nChecklist:\n"
            "  1. PostgreSQL is installed and the service is running.\n"
            "  2. DATABASE_URL in .env has the right user, password, host and port.\n"
            "  3. The user is allowed to create databases (or create it yourself:\n"
            f"     createdb -U postgres {database}).\n",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc

    return database


def apply_schema() -> None:
    sql = SCHEMA_FILE.read_text(encoding="utf-8")
    statements = _split_statements(sql)
    if not statements:
        raise SystemExit(f"No SQL statements found in {SCHEMA_FILE}")

    engine = create_engine(settings.database_url)
    try:
        with engine.begin() as connection:
            for statement in statements:
                connection.exec_driver_sql(statement)
    except OperationalError as exc:
        print(f"\nFailed to apply schema: {str(exc.orig).strip()}", file=sys.stderr)
        raise SystemExit(1) from exc
    finally:
        engine.dispose()
    print(f"  applied {len(statements)} statements from db/schema.sql")


def reset_table() -> None:
    engine = create_engine(settings.database_url)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP TABLE IF EXISTS businesses CASCADE")
    finally:
        engine.dispose()
    print("  dropped table 'businesses'")


def verify() -> None:
    engine = create_engine(settings.database_url)
    try:
        with engine.connect() as connection:
            count = connection.execute(text("SELECT COUNT(*) FROM businesses")).scalar()
        print(f"  table 'businesses' is ready ({count} rows)")
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Set up the extractor PostgreSQL database.")
    parser.add_argument("--reset", action="store_true", help="drop the businesses table first")
    parser.add_argument("--yes", action="store_true", help="skip the --reset confirmation")
    parser.add_argument("--sql-only", action="store_true", help="do not try to create the database")
    args = parser.parse_args()

    print(f"Database URL: {make_url(settings.database_url).render_as_string(hide_password=True)}")

    if not args.sql_only:
        ensure_database()

    if args.reset:
        if not args.yes:
            answer = input("This deletes ALL saved businesses. Type 'yes' to continue: ")
            if answer.strip().lower() != "yes":
                raise SystemExit("Aborted.")
        reset_table()

    apply_schema()
    verify()
    print("\nDone. Start the app with:  run.bat   (or: uvicorn app.main:app --reload)")


if __name__ == "__main__":
    main()
