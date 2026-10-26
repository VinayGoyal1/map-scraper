"""Find a business e-mail address by scanning its website.

Google Maps listings do not contain e-mail addresses, so once we have the
business website we fetch the homepage plus likely contact/about pages and
look for ``mailto:`` links, JSON-LD blocks and plain-text addresses.
"""

from __future__ import annotations

import html as html_lib
import logging
import re
from collections.abc import Callable
from urllib.parse import unquote, urljoin, urlparse

import httpx

from .config import settings

log = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,190}\.[A-Za-z]{2,24}")

# Domains that show up in website templates / analytics, never real contacts.
_SKIP_DOMAINS = {
    "example.com",
    "example.org",
    "example.net",
    "domain.com",
    "yourdomain.com",
    "email.com",
    "site.com",
    "test.com",
    "sentry.io",
    "sentry-next.io",
    "wixpress.com",
    "godaddy.com",
    "squarespace.com",
    "cloudflare.com",
    "gstatic.com",
    "google.com",
    "googlemail.com",
    "schema.org",
    "w3.org",
}

_SKIP_FILES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".css", ".js", ".pdf")

_CONTACT_HINT = re.compile(
    r"(contact|kontakt|about|impressum|support|help|team|reach|enquir|inquir|connect)",
    re.I,
)

_SOCIAL_HOSTS = (
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "twitter.com",
    "x.com",
    "youtube.com",
    "tiktok.com",
    "pinterest.com",
)

_OBFUSCATED_AT = re.compile(r"\s*(?:\[|\(|\{)\s*(?:at|AT)\s*(?:\]|\)|\})\s*")
_OBFUSCATED_DOT = re.compile(r"\s*(?:\[|\(|\{)\s*(?:dot|DOT)\s*(?:\]|\)|\})\s*")


def _normalize_site(url: str) -> str:
    url = (url or "").strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url.lstrip("/")
    parsed = urlparse(url)
    return parsed._replace(fragment="").geturl()


def _decode_cfemail(value: str) -> str | None:
    """Cloudflare 'email protection' obfuscation."""
    try:
        raw = bytes.fromhex(value)
        key = raw[0]
        return "".join(chr(byte ^ key) for byte in raw[1:])
    except (ValueError, IndexError):
        return None


def _valid_email(address: str) -> str | None:
    address = html_lib.unescape(address).strip().strip(".,;:()<>[]'\"").lower()
    if not address or address.count("@") != 1 or len(address) > 120:
        return None

    local, domain = address.split("@")
    if not local or not domain or "." not in domain:
        return None
    if domain.endswith(_SKIP_FILES):
        return None

    tld = domain.rsplit(".", 1)[-1]
    if len(tld) < 2 or not tld.isalpha():
        return None
    if domain in _SKIP_DOMAINS:
        return None
    if local.startswith(("u003", "xn--")) or re.fullmatch(r"[0-9a-f]{16,}", local):
        return None
    if "@" in local or local.startswith(".") or local.endswith("."):
        return None
    return address


def extract_emails(text: str) -> set[str]:
    """Collect candidate e-mail addresses from an HTML/text blob."""
    found: set[str] = set()
    decoded = html_lib.unescape(text or "")

    for match in re.finditer(r"mailto:([^\"'<>\s?]+)", decoded, re.I):
        candidate = _valid_email(unquote(match.group(1)))
        if candidate:
            found.add(candidate)

    for match in re.finditer(r'data-cfemail="([0-9a-fA-F]+)"', decoded):
        decoded_address = _decode_cfemail(match.group(1))
        candidate = _valid_email(decoded_address) if decoded_address else None
        if candidate:
            found.add(candidate)

    # De-obfuscate "[at]" / "(dot)" style addresses, then plain regex.
    plain = _OBFUSCATED_AT.sub("@", decoded)
    plain = _OBFUSCATED_DOT.sub(".", plain)

    for match in EMAIL_RE.finditer(plain):
        candidate = _valid_email(match.group(0))
        if candidate:
            found.add(candidate)

    return found


def _score(address: str, host: str, page_index: int) -> float:
    local, _, domain = address.partition("@")
    root = (host or "").lower().removeprefix("www.")
    score = 2.0 - 0.4 * page_index

    if root and (domain == root or domain.endswith("." + root) or root.endswith("." + domain)):
        score += 3

    if re.match(
        r"^(info|contact|hello|hi|sales|support|admin|office|enquir|inquir|team|mail|service|book)",
        local,
        re.I,
    ):
        score += 2

    if re.match(r"^(no-?reply|do-?not-?reply|postmaster|mailer-daemon|abuse)", local, re.I):
        score -= 10
    if local.isdigit() or len(local) > 40:
        score -= 3

    return score


def _contact_links(html: str, base_url: str) -> list[str]:
    links: list[str] = []
    for match in re.finditer(
        r"""<a\b[^>]*href=["']([^"']+)["'][^>]*>(.*?)</a>""", html, re.I | re.S
    ):
        href, inner = match.group(1), re.sub(r"<[^>]+>", " ", match.group(2))
        if href.lower().startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        if not (_CONTACT_HINT.search(href) or _CONTACT_HINT.search(inner)):
            continue
        absolute = urljoin(base_url, href).split("#")[0]
        if absolute not in links and absolute != base_url:
            links.append(absolute)
        if len(links) >= 4:
            break
    return links


def _same_host(first: str, second: str) -> bool:
    a = (urlparse(first).hostname or "").lower().removeprefix("www.")
    b = (urlparse(second).hostname or "").lower().removeprefix("www.")
    return bool(a) and a == b


def _is_readable(response: httpx.Response) -> bool:
    content_type = (response.headers.get("content-type") or "").lower()
    if content_type and not any(
        kind in content_type for kind in ("html", "text/plain", "text/xml", "xml")
    ):
        return False
    return True


def _fetch(client: httpx.Client, url: str) -> str | None:
    try:
        response = client.get(url)
    except httpx.HTTPError:
        # Many small-business sites have broken/expired TLS certificates.
        try:
            with httpx.Client(
                headers=client.headers,
                follow_redirects=True,
                timeout=settings.http_timeout_seconds,
                verify=False,
            ) as insecure:
                response = insecure.get(url)
        except httpx.HTTPError as exc:
            log.debug("Could not fetch %s: %s", url, exc)
            return None

    if response.status_code >= 400 or not _is_readable(response):
        return None
    return response.text[:1_500_000]


def find_emails(
    website: str,
    fallback_fetch: Callable[[str], str | None] | None = None,
) -> tuple[str | None, list[str]]:
    """Return ``(best_email, all_emails)`` for a business website.

    ``fallback_fetch`` is an optional HTML fetcher (the Playwright session)
    used when the normal HTTP client is blocked or the TLS certificate is bad.
    """
    site = _normalize_site(website)
    host = (urlparse(site).hostname or "").lower()
    if not host:
        return None, []
    if any(host == social or host.endswith("." + social) for social in _SOCIAL_HOSTS):
        return None, []

    headers = {
        "User-Agent": settings.user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    pages: list[str] = [site]
    scores: dict[str, float] = {}
    homepage: str | None = None

    with httpx.Client(
        headers=headers,
        follow_redirects=True,
        timeout=settings.http_timeout_seconds,
    ) as client:
        homepage = _fetch(client, site)
        if homepage is None and fallback_fetch is not None:
            homepage = fallback_fetch(site)
        if homepage:
            for link in _contact_links(homepage, site):
                if _same_host(link, site):
                    pages.append(link)

        # Sites whose navigation is rendered by JavaScript expose no contact
        # links in the HTML; try the two most common paths instead.
        if homepage and len(pages) == 1:
            for guess in ("contact", "contact-us"):
                candidate = urljoin(site if site.endswith("/") else site + "/", guess)
                if candidate not in pages:
                    pages.append(candidate)

        for index, page_url in enumerate(pages[:6]):
            if index == 0:
                text = homepage
            else:
                text = _fetch(client, page_url)
                if text is None and fallback_fetch is not None:
                    text = fallback_fetch(page_url)
            if not text:
                continue
            for address in extract_emails(text):
                score = _score(address, host, index)
                if address not in scores or score > scores[address]:
                    scores[address] = score

    if not scores:
        return None, []

    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    good = [address for address, score in ranked if score >= 0]
    all_found = good or [address for address, _ in ranked]
    return (all_found[0] if all_found else None), all_found
