"""FastAPI application: scrape a Google Maps link, store it, serve the data."""

from __future__ import annotations

import csv
import io
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from . import crud
from .config import settings
from .database import get_db
from .extractor import ExtractionError, InvalidMapsUrl, extract_business
from .schemas import (
    BusinessList,
    BusinessOut,
    ExtractRequest,
    ExtractResponse,
    Stats,
)

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
log = logging.getLogger("app")

STATIC_DIR = Path(__file__).resolve().parent / "static"
DB_HINT = "Run `python db/init_db.py` to create the database and tables."


@asynccontextmanager
async def lifespan(app: FastAPI):
    from .database import engine

    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        log.info("Connected to PostgreSQL")
    except Exception as exc:  # noqa: BLE001 - report, don't crash the UI
        log.error("PostgreSQL is not reachable: %s", exc)
        log.error("Fix DATABASE_URL in .env and run: python db/init_db.py")
    yield


app = FastAPI(
    title="Google Maps Business Extractor",
    version="1.0.0",
    description=(
        "Paste a Google Maps business link, extract name / phone / website / "
        "email / map link, and store everything in PostgreSQL."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ---------------------------------------------------------------------------
# Error handling: turn database problems into actionable messages
# ---------------------------------------------------------------------------
@app.exception_handler(OperationalError)
async def _operational_error_handler(request, exc):  # noqa: ANN001
    log.error("Database error: %s", exc)
    return JSONResponse(
        status_code=503,
        content={
            "detail": "Cannot talk to PostgreSQL. Is the server running and is DATABASE_URL correct?",
            "hint": DB_HINT,
        },
    )


@app.exception_handler(ProgrammingError)
async def _programming_error_handler(request, exc):  # noqa: ANN001
    log.error("Database schema error: %s", exc)
    return JSONResponse(
        status_code=503,
        content={
            "detail": "The database schema is missing or out of date (table `businesses`?).",
            "hint": DB_HINT,
        },
    )


@app.exception_handler(DBAPIError)
async def _dbapi_error_handler(request, exc):  # noqa: ANN001
    log.error("Database error: %s", exc)
    return JSONResponse(
        status_code=503,
        content={"detail": "Database error while handling the request.", "hint": DB_HINT},
    )


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
def index():
    page = STATIC_DIR / "index.html"
    if not page.is_file():
        return JSONResponse({"detail": "UI not found. API docs: /docs"})
    return FileResponse(page)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
@app.get("/api/health", tags=["system"])
def health():
    from .database import engine

    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": "ok", "database": True}
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "degraded",
            "database": False,
            "error": str(exc).splitlines()[0],
            "hint": DB_HINT,
        }


@app.post("/api/extract", response_model=ExtractResponse, tags=["extraction"])
def extract(payload: ExtractRequest, db: Session = Depends(get_db)):
    """Scrape a Google Maps link, enrich it with an email, and save it.

    Takes roughly 8-25 seconds (browser + website crawl). If the business is
    already stored it is refreshed instead of duplicated.
    """
    started = time.perf_counter()
    try:
        result = extract_business(payload.url)
    except InvalidMapsUrl as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ExtractionError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.exception("Unexpected extraction failure")
        raise HTTPException(
            status_code=500, detail=f"Unexpected error while extracting: {exc}"
        ) from exc

    business, created = crud.upsert_business(db, result)
    return ExtractResponse(
        created=created,
        duration_seconds=round(time.perf_counter() - started, 2),
        business=BusinessOut.model_validate(business),
    )


@app.get("/api/businesses", response_model=BusinessList, tags=["businesses"])
def list_businesses(
    q: str | None = Query(None, max_length=200, description="Search name/phone/email/site/address"),
    has_website: bool | None = Query(None),
    has_email: bool | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    total, rows = crud.list_businesses(
        db,
        query=q,
        has_website=has_website,
        has_email=has_email,
        limit=limit,
        offset=offset,
    )
    return BusinessList(
        total=total,
        limit=limit,
        offset=offset,
        items=[BusinessOut.model_validate(row) for row in rows],
    )


@app.get("/api/businesses/{business_id}", response_model=BusinessOut, tags=["businesses"])
def get_business(business_id: int, db: Session = Depends(get_db)):
    business = crud.get_business(db, business_id)
    if not business:
        raise HTTPException(status_code=404, detail="Business not found")
    return BusinessOut.model_validate(business)


@app.delete("/api/businesses/{business_id}", status_code=204, tags=["businesses"])
def delete_business(business_id: int, db: Session = Depends(get_db)):
    if not crud.delete_business(db, business_id):
        raise HTTPException(status_code=404, detail="Business not found")
    return None


@app.get("/api/stats", response_model=Stats, tags=["businesses"])
def get_stats(db: Session = Depends(get_db)):
    return Stats(**crud.stats(db))


@app.get("/api/export.csv", tags=["businesses"])
def export_csv(db: Session = Depends(get_db)):
    """Download every saved business as a CSV file (Excel friendly)."""
    _, rows = crud.list_businesses(db, limit=100_000, offset=0)

    columns = [
        "id",
        "name",
        "phone",
        "has_website",
        "website",
        "email",
        "emails_found",
        "address",
        "category",
        "rating",
        "reviews_count",
        "map_url",
        "place_id",
        "created_at",
    ]

    def generate():
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        buffer.write("\ufeff")  # BOM so Excel detects UTF-8
        writer.writerow(columns)
        yield buffer.getvalue()

        for row in rows:
            buffer.seek(0)
            buffer.truncate(0)
            writer.writerow(
                [
                    row.id,
                    row.name,
                    row.phone or "",
                    "yes" if row.has_website else "no",
                    row.website or "",
                    row.email or "",
                    "; ".join(row.emails_found or []),
                    row.address or "",
                    row.category or "",
                    row.rating if row.rating is not None else "",
                    row.reviews_count if row.reviews_count is not None else "",
                    row.map_url,
                    row.place_id or "",
                    row.created_at.isoformat() if row.created_at else "",
                ]
            )
            yield buffer.getvalue()

    return StreamingResponse(
        generate(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="businesses.csv"'},
    )
