# Google Maps Business Extractor

Paste a Google Maps link → the tool opens the place in a real browser, reads the
business details, hunts for an e-mail address on the business website, saves
everything in PostgreSQL, and serves it through a small web UI + REST API.

```
 link ──► Playwright (Chromium) ──► business data ──► email crawl ──► PostgreSQL ──► FastAPI / UI
```

## What it extracts

| Field | Where it comes from |
|---|---|
| **Name** | Place panel title |
| **Phone** | Phone button of the Maps listing |
| **Has website?** | `true` / `false` flag |
| **Website** | Website button of the listing (tracking params stripped) |
| **Email** | Business website: homepage, contact pages, `mailto:` links, obfuscated `[at]` addresses |
| **Map link** | Resolved / cleaned Google Maps place URL |
| *Extras* | Address, category, rating, review count, coordinates, place id |

> Google Maps listings do **not** contain e-mail addresses. The email field is
> filled by crawling the business website, so it is legitimately empty for
> listings without a website or with a contact-form-only site.

## Project layout

```
extracter/
├── app/
│   ├── main.py            FastAPI app: routes, errors, CSV export
│   ├── extractor.py       Playwright scraper (name/phone/website/map link)
│   ├── email_finder.py    Website e-mail crawler
│   ├── crud.py            Database queries + upsert/dedupe
│   ├── models.py          SQLAlchemy model
│   ├── schemas.py         Request/response models
│   ├── config.py          Settings from .env
│   ├── database.py        Engine + session
│   └── static/index.html  Web UI
├── db/
│   ├── schema.sql         ← table + indexes (the database definition)
│   ├── init_db.py         ← creates the database and applies schema.sql
│   └── setup_database.sql ← one-shot psql version
├── .env                   Your settings (edit the DB password!)
├── requirements.txt
└── run.bat                One-click start (installs deps on first run)
```

## Quick start (Windows)

**1. Make sure PostgreSQL is running.** You need a user + password that can
create a database.

**2. Set your database connection** in `.env`:

```ini
DATABASE_URL=postgresql+psycopg://postgres:YOUR_PASSWORD@localhost:5432/business_extractor
```

**3. Install dependencies** (skip if you just use `run.bat`):

```bat
py -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m playwright install chromium
```

**4. Create the database and tables:**

```bat
.venv\Scripts\python.exe db\init_db.py
```

This connects to the `postgres` maintenance database, creates
`business_extractor` if it is missing, and applies `db/schema.sql`.

**5. Start the app:**

```bat
run.bat
```

Open **http://127.0.0.1:8000** — paste a link, press *Extract & save*.
API docs live at **http://127.0.0.1:8000/docs**.

## Database setup (the separate file you asked for)

All DDL lives in **`db/schema.sql`**. Three ways to apply it:

| Method | Command |
|---|---|
| Recommended | `python db/init_db.py` (creates DB + tables, idempotent) |
| Plain psql | `psql -U postgres -f db/setup_database.sql` |
| pgAdmin | Create a database, then run the contents of `db/schema.sql` |

Other useful commands:

```bat
python db\init_db.py --reset          :: drop the table and recreate it (asks first)
python db\init_db.py --reset --yes    :: same, no prompt
python db\init_db.py --sql-only       :: only apply schema.sql to the existing DB
```

### Table `businesses`

| Column | Type | Notes |
|---|---|---|
| `id` | `BIGSERIAL` | primary key |
| `name` | `TEXT NOT NULL` | business name |
| `phone` | `TEXT` | as shown on Maps |
| `website` | `TEXT` | cleaned URL |
| `has_website` | `BOOLEAN` | quick filter |
| `email` | `TEXT` | best candidate |
| `emails_found` | `TEXT[]` | every address found |
| `address`, `category` | `TEXT` | |
| `rating`, `reviews_count` | `DOUBLE PRECISION`, `INTEGER` | |
| `latitude`, `longitude` | `DOUBLE PRECISION` | |
| `place_id` | `TEXT` | Google place id (`0x...:0x...`) |
| `map_url` | `TEXT NOT NULL` | resolved business link |
| `source_url` | `TEXT` | the link you pasted |
| `raw_data` | `JSONB` | resolved URL, timings, extra emails |
| `created_at`, `updated_at` | `TIMESTAMPTZ` | |

Deduplication: a place is matched by `place_id` first, then by `map_url`, so
re-extracting the same business updates the row instead of duplicating it
(unique indexes back this up).

`updated_at` is refreshed by the `trg_businesses_updated_at` trigger, so rows
edited directly with psql or pgAdmin get a correct timestamp too. `schema.sql`
is safe to re-run and also adds any missing columns to an older table.

## API

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/extract` | Scrape + save. Body: `{"url": "https://maps.app.goo.gl/..."}` |
| `GET` | `/api/businesses` | List; params: `q`, `has_website`, `has_email`, `limit`, `offset` |
| `GET` | `/api/businesses/{id}` | One business |
| `DELETE` | `/api/businesses/{id}` | Delete |
| `GET` | `/api/stats` | Totals (all / with site / without site / with email) |
| `GET` | `/api/export.csv` | Download everything as CSV |
| `GET` | `/api/health` | App + database status |

```bash
curl -X POST http://127.0.0.1:8000/api/extract \
  -H "Content-Type: application/json" \
  -d "{\"url\": \"https://maps.app.goo.gl/m1ygmuWxLeQAh7Ct8\"}"

curl "http://127.0.0.1:8000/api/businesses?has_website=true&limit=20"
```

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/extract `
  -ContentType "application/json" `
  -Body (@{ url = "https://maps.app.goo.gl/m1ygmuWxLeQAh7Ct8" } | ConvertTo-Json)
```

`POST /api/extract` returns `{"created": true|false, "duration_seconds": 8.4, "business": {...}}`.
A single extraction takes roughly **8–20 seconds** (browser + website crawl).

## How it works

1. **Validate** the URL (only Google Maps domains / `goo.gl` short links).
2. **Expand** `maps.app.goo.gl` short links and force `hl=en` so labels are stable.
3. **Open** the place with Chromium, dismiss the cookie wall if it appears, and
   click the first result when the link is a search/list URL.
4. **Read** the place panel (`h1`, `data-item-id` attributes, rating block).
5. **Find an email** by fetching the business website with `httpx`; if the site
   blocks plain HTTP clients (403/Cloudflare), the already-open browser fetches
   it instead. It checks the homepage, contact/about links, `/contact`,
   `/contact-us`, `mailto:` links, Cloudflare-obfuscated addresses and
   `[at]`/`(dot)` style text.
6. **Upsert** into PostgreSQL and return the saved row.

Tuning is in `.env`:

```ini
HEADLESS=true              # false = watch the browser work (debugging)
EXTRACT_MAX_CONCURRENCY=2  # parallel extractions (keep low)
NAV_TIMEOUT_MS=30000
HTTP_TIMEOUT_SECONDS=12
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| API returns 503 “Cannot talk to PostgreSQL” | Start PostgreSQL, fix `DATABASE_URL` in `.env`, run `python db/init_db.py` |
| `password authentication failed` | The password in `.env` is wrong. Use the one you set when installing PostgreSQL |
| `Could not start the Chromium browser` | `.venv\Scripts\python.exe -m playwright install chromium` |
| “Google Maps did not open a business panel” | The link may point to a list/search, Google may be rate-limiting, or the layout changed. Retry, or set `HEADLESS=false` and watch |
| Email is empty | Normal for contact-form-only sites and listings without a website. Check `emails_found` after re-running |
| Extraction seems slow | Blocked sites cost extra time because the browser is used for the website crawl too |

## Notes

- Google Maps is scraped through a real browser; selectors can change when
  Google ships a redesign. All of them are grouped in `app/extractor.py`
  (`_read_place`, `_place_name`) if you need to update them.
- Automated scraping of Google Maps can conflict with Google's Terms of
  Service. Use this for personal/research projects, keep concurrency low, and
  prefer the official Google Places API if you need this at scale.
- Marketing parameters (`utm_*`, `gclid`, …) are stripped from saved websites.
- Map URLs are stored as Google returns them, so they can be long — that is
  what makes them open the exact place.
