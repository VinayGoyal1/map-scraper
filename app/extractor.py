"""Scrape a Google Maps place page.

The scraper drives a real Chromium browser (Playwright) because Google Maps
renders its place panel with JavaScript.  It extracts:

    name, phone, website, address, category, rating, reviews,
    coordinates, place id, canonical map link

Email addresses are not part of a Maps listing, so the business website
(if any) is crawled separately by ``app.email_finder``.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlunparse

import httpx
from playwright.sync_api import Page, sync_playwright

from .config import settings
from .email_finder import find_emails

log = logging.getLogger(__name__)

_GOOGLE_HOST_RE = re.compile(r"(^|\.)google\.[a-z]{2,4}(\.[a-z]{2,4})?$", re.I)
_COORD_RE = re.compile(r"/@(-?\d{1,3}(?:\.\d+)?),(-?\d{1,3}(?:\.\d+)?)")
_DATA_COORD_RE = re.compile(r"!3d(-?\d{1,3}(?:\.\d+)?)!4d(-?\d{1,3}(?:\.\d+)?)")
_PLACE_ID_RE = re.compile(r"!1s(0x[0-9a-fA-F]+:0x[0-9a-fA-F]+)")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}")

# Google asks for cookie consent in some regions; this cookie skips the wall.
_CONSENT_COOKIE = {
    "name": "CONSENT",
    "value": "YES+cb.20240101-00-p0.en+FX+410",
    "domain": ".google.com",
    "path": "/",
}

# Limit how many browser sessions run at once (Google dislikes bursts).
_inflight = threading.BoundedSemaphore(max(1, int(settings.extract_max_concurrency)))


class ExtractionError(RuntimeError):
    """Google Maps could not be scraped (blocked, layout change, bad link...)."""


class InvalidMapsUrl(ExtractionError):
    """The supplied string is not a Google Maps URL."""


@dataclass
class ExtractionResult:
    name: str
    phone: str | None = None
    website: str | None = None
    email: str | None = None
    emails_found: list[str] = field(default_factory=list)
    address: str | None = None
    category: str | None = None
    rating: float | None = None
    reviews_count: int | None = None
    latitude: float | None = None
    longitude: float | None = None
    place_id: str | None = None
    map_url: str = ""
    source_url: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def has_website(self) -> bool:
        return bool(self.website)


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------
def with_scheme(value: str) -> str:
    """Turn 'google.com/maps/...' into 'https://google.com/maps/...'."""
    value = (value or "").strip()
    if not value:
        raise InvalidMapsUrl("No URL supplied.")
    if not re.match(r"^https?://", value, re.I):
        value = "https://" + value.lstrip("/")
    return value


def looks_like_google_maps_url(value: str) -> bool:
    try:
        parsed = urlparse(with_scheme(value))
    except InvalidMapsUrl:
        return False

    host = (parsed.hostname or "").lower()
    if not host:
        return False

    if host == "goo.gl" or host.endswith(".goo.gl"):
        return True
    if not _GOOGLE_HOST_RE.search(host):
        return False

    if "/maps" in (parsed.path or ""):
        return True
    # e.g. https://www.google.com/?cid=12345
    query = parse_qs(parsed.query)
    return bool({"cid", "q", "query", "ll", "daddr"} & set(query))


def _add_english(url: str) -> str:
    """Force English UI so aria-labels contain 'Phone:' / 'Website:'."""
    parsed = urlparse(url)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if not any(key == "hl" for key, _ in query):
        query.append(("hl", "en"))
    return urlunparse(parsed._replace(query=urlencode(query)))


def clean_map_url(url: str, place_id: str | None) -> str:
    """Drop tracking parameters but keep the place reference."""
    parsed = urlparse(url)
    kept = [(k, v) for k, v in parse_qsl(parsed.query) if k.lower() in {"cid", "q"}]
    if "/maps/place" not in (parsed.path or "") and place_id:
        return f"https://www.google.com/maps/place/?q=place_id:{place_id}"
    return urlunparse(parsed._replace(query=urlencode(kept), fragment=""))


def _resolve_short_link(url: str) -> str:
    """Expand maps.app.goo.gl / goo.gl links (Playwright can do it too, but this
    gives us the final URL up front)."""
    host = (urlparse(url).hostname or "").lower()
    if not (host == "goo.gl" or host.endswith(".goo.gl")):
        return url
    try:
        with httpx.Client(
            follow_redirects=True,
            timeout=15,
            headers={"User-Agent": settings.user_agent},
        ) as client:
            response = client.get(url)
            final = str(response.url)
            if "/maps" in final and "google" in (urlparse(final).hostname or ""):
                return final
    except Exception as exc:  # noqa: BLE001 - fall back to browser navigation
        log.debug("Could not expand short link %s: %s", url, exc)
    return url


def _normalize_website(href: str | None) -> str | None:
    if not href:
        return None
    href = href.strip()
    if not href:
        return None

    parsed = urlparse(href)
    # Google sometimes wraps outbound links in /url?q=<target>
    if parsed.path == "/url" and parsed.hostname and "google." in parsed.hostname:
        target = parse_qs(parsed.query).get("q") or parse_qs(parsed.query).get("url")
        if target:
            href = target[0]

    if not re.match(r"^https?://", href, re.I):
        href = "https://" + href.lstrip("/")

    parsed = urlparse(href)
    if not parsed.hostname:
        return None

    # Drop marketing parameters; they are noise in a database.
    tracking_prefixes = ("utm_", "gclid", "fbclid", "gad_source", "mc_cid", "mc_eid")
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith(tracking_prefixes)
    ]
    return urlunparse(parsed._replace(query=urlencode(query), fragment=""))


# ---------------------------------------------------------------------------
# Browser scraping
# ---------------------------------------------------------------------------
def _first_text(page: Page, selectors: list[str]) -> str | None:
    for selector in selectors:
        locator = page.locator(selector)
        try:
            count = locator.count()
        except Exception:  # noqa: BLE001
            continue
        for index in range(min(count, 3)):
            try:
                text = (locator.nth(index).inner_text(timeout=1500) or "").strip()
            except Exception:  # noqa: BLE001
                continue
            if text:
                return text
    return None


def _first_attr(page: Page, selectors: list[str], attribute: str) -> str | None:
    for selector in selectors:
        locator = page.locator(selector)
        try:
            count = locator.count()
        except Exception:  # noqa: BLE001
            continue
        for index in range(min(count, 3)):
            try:
                value = locator.nth(index).get_attribute(attribute, timeout=1500)
            except Exception:  # noqa: BLE001
                continue
            if value and value.strip():
                return value.strip()
    return None


def _place_name(page: Page) -> str | None:
    """The panel title. Maps keeps an empty ``h1.fontHeadlineLarge`` in the DOM,
    so every match has to be checked, not just the first."""
    ignored = {"", "results", "google maps"}
    for selector in (
        "h1.DUwDvf",
        "h1.fontHeadlineLarge",
        "div[role='main'] h1",
        "h1",
    ):
        locator = page.locator(selector)
        try:
            count = locator.count()
        except Exception:  # noqa: BLE001
            continue
        for index in range(min(count, 6)):
            try:
                text = (locator.nth(index).inner_text(timeout=1500) or "").strip()
            except Exception:  # noqa: BLE001
                continue
            if text and text.lower() not in ignored:
                return text
    return None


def _dismiss_consent(page: Page) -> bool:
    """Click through Google's cookie/consent wall if present."""
    clicked = False
    patterns = ["Accept all", "I agree", "Accept", "Agree to all"]
    for pattern in patterns:
        try:
            button = page.get_by_role("button", name=re.compile(pattern, re.I)).first
            if button.count() and button.is_visible():
                button.click(timeout=3000)
                page.wait_for_timeout(1500)
                clicked = True
                break
        except Exception:  # noqa: BLE001
            continue
    return clicked


def _wait_for_place(page: Page, timeout_seconds: float = 25) -> bool:
    """Wait for the place panel; click the first list result if we landed on a
    search results page."""
    deadline = time.time() + timeout_seconds
    tried_result_click = False

    while time.time() < deadline:
        if _place_name(page):
            return True

        if not tried_result_click:
            links = page.locator('a[href*="/maps/place/"]')
            if links.count():
                try:
                    links.first.click(timeout=5000)
                    tried_result_click = True
                    page.wait_for_timeout(2500)
                    continue
                except Exception:  # noqa: BLE001
                    tried_result_click = True

        page.wait_for_timeout(700)

    return False


def _read_place(page: Page) -> dict:
    """Read every field off the currently open place panel."""
    page.wait_for_timeout(800)

    name = _place_name(page)
    if not name:
        raise ExtractionError(
            "Could not read the business name. The link may point to a list of "
            "results instead of a single business, or Google changed its layout."
        )

    phone = None
    phone_node = page.locator('[data-item-id^="phone:tel:"]')  # button or anchor
    try:
        if phone_node.count():
            raw_id = phone_node.first.get_attribute("data-item-id", timeout=2000) or ""
            if "phone:tel:" in raw_id:
                phone = raw_id.split("phone:tel:", 1)[1].strip()
    except Exception:  # noqa: BLE001
        pass
    if not phone:
        label = _first_attr(
            page, ['button[aria-label^="Phone:"]', 'a[aria-label^="Phone:"]'], "aria-label"
        )
        if label and ":" in label:
            phone = label.split(":", 1)[1].strip()
    if not phone:
        tel = _first_attr(page, ['a[href^="tel:"]'], "href")
        if tel:
            phone = tel.split(":", 1)[-1].lstrip("/").strip()
    phone = re.sub(r"\s{2,}", " ", phone).strip() if phone else None

    website = _normalize_website(
        _first_attr(
            page,
            ['a[data-item-id="authority"]', '[data-item-id="authority"]', 'a[aria-label^="Website:"]'],
            "href",
        )
    )

    address = _first_attr(
        page, ['[data-item-id="address"]', 'button[aria-label^="Address:"]'], "aria-label"
    )
    if address and address.lower().startswith("address:"):
        address = address.split(":", 1)[1].strip()

    category = _first_text(
        page,
        [
            'button[jsaction*="pane.rating.category"]',
            "button.DkEaL",
            "div.DkEaL",
        ],
    )

    rating = None
    rating_label = _first_attr(
        page,
        ['div.F7nice span[role="img"][aria-label*="star"]', 'span[aria-label*="star"]'],
        "aria-label",
    )
    if rating_label:
        match = re.search(r"(\d+(?:[.,]\d+)?)", rating_label)
        if match:
            rating = float(match.group(1).replace(",", "."))
    if rating is None:
        stars = _first_text(page, ["div.F7nice span[aria-hidden='true']", "span.ceNzKf"])
        if stars:
            match = re.search(r"(\d+(?:[.,]\d+)?)", stars)
            if match:
                rating = float(match.group(1).replace(",", "."))

    reviews_count = None
    reviews_label = _first_attr(
        page, ['div.F7nice span[aria-label*="review"]', 'span[aria-label*="review"]'], "aria-label"
    )
    digits = re.sub(r"[^\d]", "", reviews_label or "")
    if digits:
        reviews_count = int(digits)
    else:
        summary = _first_text(page, ["div.F7nice"])
        if summary:
            match = re.search(r"\(([\d.,\s]+)\)", summary)
            if match:
                digits = re.sub(r"[^\d]", "", match.group(1))
                if digits:
                    reviews_count = int(digits)

    page_url = page.url
    latitude = longitude = None
    match = _COORD_RE.search(page_url) or _DATA_COORD_RE.search(page_url)
    if match:
        try:
            latitude, longitude = float(match.group(1)), float(match.group(2))
        except ValueError:
            pass

    place_id = None
    match = _PLACE_ID_RE.search(page_url)
    if match:
        place_id = match.group(1)

    panel_text = ""
    try:
        panel = page.locator("div[role='main']")
        if panel.count():
            panel_text = panel.first.inner_text(timeout=3000)[:25_000]
    except Exception:  # noqa: BLE001
        pass

    return {
        "name": name,
        "phone": phone,
        "website": website,
        "address": address,
        "category": category,
        "rating": rating,
        "reviews_count": reviews_count,
        "latitude": latitude,
        "longitude": longitude,
        "place_id": place_id,
        "map_url": clean_map_url(page_url, place_id),
        "resolved_url": page_url,
        "panel_text": panel_text,
    }


class MapsBrowser:
    """A Chromium session reused for the Maps page and website e-mail lookups.

    Some business sites reject plain HTTP clients (Cloudflare/WAF), so the
    already-open browser is used as a fallback when fetching their HTML.
    """

    def __init__(self) -> None:
        self._playwright = None
        self._browser = None
        self._context = None

    def __enter__(self) -> "MapsBrowser":
        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(
                headless=settings.headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
            )
            self._context = self._browser.new_context(
                user_agent=settings.user_agent,
                locale="en-US",
                viewport={"width": 1440, "height": 900},
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
            self._context.add_cookies([_CONSENT_COOKIE])
            return self
        except Exception as exc:  # noqa: BLE001
            self.__exit__()
            raise ExtractionError(
                "Could not start the Chromium browser. Install it once with: "
                "`.venv\\Scripts\\python.exe -m playwright install chromium`"
            ) from exc

    def __exit__(self, *exc_info) -> None:  # noqa: ANN002
        try:
            if self._browser is not None:
                self._browser.close()
        finally:
            if self._playwright is not None:
                self._playwright.stop()

    def read_place(self, url: str) -> dict:
        page = self._context.new_page()
        page.set_default_timeout(10_000)
        try:
            target = _add_english(url)
            page.goto(target, wait_until="domcontentloaded", timeout=settings.nav_timeout_ms)

            if _dismiss_consent(page):
                page.goto(target, wait_until="domcontentloaded", timeout=settings.nav_timeout_ms)

            if "consent.google." in (page.url or ""):
                _dismiss_consent(page)
                page.goto(target, wait_until="domcontentloaded", timeout=settings.nav_timeout_ms)

            if not _wait_for_place(page):
                raise ExtractionError(
                    "Google Maps did not open a business panel. Check that the link "
                    "points to a single place, that you are online, and try again."
                )

            return _read_place(page)
        finally:
            page.close()

    def fetch_html(self, url: str) -> str | None:
        page = self._context.new_page()
        try:
            page.set_default_timeout(10_000)
            page.goto(url, wait_until="domcontentloaded", timeout=settings.nav_timeout_ms)
            page.wait_for_timeout(1500)
            return page.content()[:1_500_000]
        except Exception as exc:  # noqa: BLE001 - best effort only
            log.debug("Browser fetch failed for %s: %s", url, exc)
            return None
        finally:
            page.close()


def _emails_from_text(text: str) -> list[str]:
    """Very small fallback: pick e-mails visible in the Maps page itself."""
    found = []
    for raw in _EMAIL_RE.findall(text or ""):
        address = raw.strip(".,;:()<>[]'\"").lower()
        domain = address.split("@")[-1]
        if domain.endswith((".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".js", ".css")):
            continue
        if any(bad in domain for bad in ("google.com", "gstatic.com", "example.com", "sentry.io")):
            continue
        found.append(address)
    return sorted(set(found))


def extract_business(url: str) -> ExtractionResult:
    """Full pipeline: URL -> Maps scrape -> website email lookup."""
    started = time.time()
    source_url = with_scheme(url)

    if not looks_like_google_maps_url(source_url):
        raise InvalidMapsUrl(
            "That is not a Google Maps link. Paste something like "
            "https://maps.app.goo.gl/xxxx or https://www.google.com/maps/place/..."
        )

    resolved = _resolve_short_link(source_url)

    if not _inflight.acquire(timeout=120):
        raise ExtractionError("Too many extractions are running. Please retry in a moment.")
    try:
        with MapsBrowser() as session:
            data = session.read_place(resolved)
            website = data["website"]
            email, emails_found = (None, [])
            if website:
                try:
                    # The browser is passed as a fallback for sites that block
                    # plain HTTP clients (Cloudflare, 403, broken TLS...).
                    email, emails_found = find_emails(
                        website, fallback_fetch=session.fetch_html
                    )
                except Exception as exc:  # noqa: BLE001 - never fail extraction over email
                    log.warning("Email lookup failed for %s: %s", website, exc)
    finally:
        _inflight.release()

    maps_emails: list[str] = []
    if not email and data["panel_text"]:
        maps_emails = _emails_from_text(data["panel_text"])
        if maps_emails:
            email = maps_emails[0]
            emails_found = sorted(set(emails_found) | set(maps_emails))

    result = ExtractionResult(
        name=data["name"],
        phone=data["phone"],
        website=website,
        email=email,
        emails_found=emails_found,
        address=data["address"],
        category=data["category"],
        rating=data["rating"],
        reviews_count=data["reviews_count"],
        latitude=data["latitude"],
        longitude=data["longitude"],
        place_id=data["place_id"],
        map_url=data["map_url"],
        source_url=source_url,
        raw={
            "resolved_url": data["resolved_url"],
            "maps_page_emails": maps_emails,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "scrape_seconds": round(time.time() - started, 2),
        },
    )
    return result
