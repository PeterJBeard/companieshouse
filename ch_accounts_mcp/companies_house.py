"""Companies House Public Data API and Document API client.

The API key (COMPANIES_HOUSE_API_KEY) is sent as HTTP basic auth username with an empty password.
It is never logged and never forwarded to the pre-signed S3 URL the Document API redirects to.
Without a key, documents can still be fetched from the public find-and-update site.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from html import unescape
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from . import __version__
from .cache import Cache, get_cache

log = logging.getLogger(__name__)

API = "https://api.company-information.service.gov.uk"
DOC_API = "https://document-api.company-information.service.gov.uk"
PUBLIC = "https://find-and-update.company-information.service.gov.uk"
USER_AGENT = f"ch-accounts-mcp/{__version__}"

RATE_CAPACITY = 600
RATE_WINDOW_S = 300


class CompaniesHouseError(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def public_document_url(company_number: str, transaction_id: str) -> str:
    return f"{PUBLIC}/company/{company_number}/filing-history/{transaction_id}/document?format=pdf&download=0"


def public_filing_url(company_number: str) -> str:
    return f"{PUBLIC}/company/{company_number}/filing-history"


def redact(text: str, secret: str | None) -> str:
    return text.replace(secret, "[REDACTED]") if secret else text


class CompaniesHouseClient:
    def __init__(self, api_key: str | None, cache: Cache | None = None, timeout: float = 60.0):
        self._key = api_key
        self._cache = cache
        self._http = httpx.Client(timeout=timeout, headers={"User-Agent": USER_AGENT}, follow_redirects=False)

    @property
    def has_key(self) -> bool:
        return bool(self._key)

    def _throttle(self) -> None:
        cache = self._cache or get_cache()
        while True:
            wait = cache.take_rate_token("companies_house", RATE_CAPACITY, RATE_CAPACITY / RATE_WINDOW_S, time.time())
            if wait <= 0:
                return
            time.sleep(min(wait, 5))

    def _get(self, url: str, params: dict | None = None, accept: str = "application/json") -> httpx.Response:
        if not self._key:
            raise CompaniesHouseError("COMPANIES_HOUSE_API_KEY is not set; use pdf_path, pdf_url or company_number + transaction_id (public site) instead")
        self._throttle()
        try:
            r = self._http.get(url, params=params, auth=(self._key, ""), headers={"Accept": accept})
        except httpx.HTTPError as e:
            raise CompaniesHouseError(redact(f"request failed: {type(e).__name__}", self._key)) from None
        if r.status_code == 429:
            time.sleep(5)
            return self._get(url, params, accept)
        if r.status_code in (301, 302, 303, 307, 308):
            return r
        if r.status_code >= 400:
            raise CompaniesHouseError(f"Companies House returned HTTP {r.status_code} for {urlparse(url).path}")
        return r

    # ------------------------------------------------------------------ API

    def search(self, query: str, items: int = 20) -> list[dict]:
        r = self._get(f"{API}/search/companies", {"q": query, "items_per_page": items}).json()
        return [
            {
                "company_number": i.get("company_number"),
                "name": i.get("title"),
                "status": i.get("company_status"),
                "incorporation_date": i.get("date_of_creation"),
                "company_type": i.get("company_type"),
                "address": i.get("address_snippet"),
            }
            for i in r.get("items", [])
        ]

    def profile(self, company_number: str) -> dict:
        return self._get(f"{API}/company/{company_number}").json()

    def accounts_filings(self, company_number: str) -> list[dict]:
        r = self._get(f"{API}/company/{company_number}/filing-history", {"category": "accounts", "items_per_page": 100}).json()
        out = []
        for i in r.get("items", []):
            dv = i.get("description_values") or {}
            out.append(
                {
                    "transaction_id": i.get("transaction_id"),
                    "date": i.get("date"),
                    "type": i.get("type"),
                    "description": i.get("description"),
                    "accounts_type": _accounts_type(i.get("description", "")),
                    "made_up_date": dv.get("made_up_date"),
                    "pages": i.get("pages"),
                    "document_metadata": (i.get("links") or {}).get("document_metadata"),
                    "source": "api",
                }
            )
        return out

    def document_metadata(self, metadata_url: str) -> dict:
        url = metadata_url if metadata_url.startswith("http") else urljoin(DOC_API, metadata_url)
        return self._get(url).json()

    def fetch_document(self, metadata_url: str, accept: str = "application/pdf") -> bytes:
        """GET {metadata}/content, then follow the S3 redirect without the Authorization header."""
        url = (metadata_url if metadata_url.startswith("http") else urljoin(DOC_API, metadata_url)).rstrip("/")
        if not url.endswith("/content"):
            url += "/content"
        r = self._get(url, accept=accept)
        hops = 0
        while r.status_code in (301, 302, 303, 307, 308) and hops < 5:
            loc = r.headers.get("location")
            if not loc:
                break
            # fresh request, no auth: S3 rejects the Companies House credentials
            r = httpx.get(urljoin(url, loc), headers={"Accept": accept, "User-Agent": USER_AGENT}, timeout=120, follow_redirects=False)
            hops += 1
        if r.status_code >= 400:
            raise CompaniesHouseError(f"document download failed with HTTP {r.status_code}")
        return r.content

    def filing_for(self, company_number: str, year_end: str) -> dict | None:
        """Pick the accounts filing whose made-up date matches year_end (YYYY or YYYY-MM-DD)."""
        filings = self.accounts_filings(company_number) if self.has_key else public_accounts_filings(company_number)
        return pick_filing(filings, year_end)

    def close(self) -> None:
        self._http.close()


def pick_filing(filings: list[dict], year_end: str) -> dict | None:
    cands = [f for f in filings if (f.get("type") or "").startswith("AA") and f.get("made_up_date")]
    exact = [f for f in cands if f["made_up_date"] == year_end]
    if exact:
        return exact[0]
    yr = [f for f in cands if f["made_up_date"].startswith(year_end[:4])]
    return yr[0] if yr else None


def _accounts_type(desc: str) -> str | None:
    m = re.search(r"accounts-with-accounts-type-([a-z-]+)", desc or "")
    return m.group(1) if m else None


# --------------------------------------------------------------------------- no-key public site

_MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"], 1)}


def _parse_long_date(s: str) -> str | None:
    m = re.search(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", s)
    if not m or m.group(2).lower() not in _MONTHS:
        return None
    return f"{m.group(3)}-{_MONTHS[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"


def _parse_short_date(s: str) -> str | None:
    try:
        return datetime.strptime(s.strip(), "%d %b %Y").date().isoformat()
    except ValueError:
        return _parse_long_date(s)


def public_accounts_filings(company_number: str, pages: int = 3) -> list[dict]:
    """Scrape the public filing history (no API key). Fragile by nature; the API is preferred."""
    out: list[dict] = []
    with httpx.Client(timeout=30, headers={"User-Agent": USER_AGENT}, follow_redirects=True) as c:
        for p in range(1, pages + 1):
            r = c.get(public_filing_url(company_number), params={"category": "accounts", "page": p})
            if r.status_code != 200:
                break
            rows = re.findall(r"<tr>(.*?)</tr>", r.text, re.S)
            found = 0
            for tr in rows:
                tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
                if len(tds) < 4:
                    continue
                text = lambda h: " ".join(unescape(re.sub(r"<[^>]+>", " ", h)).split())
                typ = text(tds[1])
                if not typ.startswith("AA"):
                    continue
                desc = text(tds[2])
                m = re.search(r"/filing-history/([^/]+)/document\?format=pdf", tds[3])
                pg = re.search(r"\((\d+) pages?\)", tds[3])
                found += 1
                out.append(
                    {
                        "transaction_id": m.group(1) if m else None,
                        "date": _parse_short_date(text(tds[0])),
                        "type": typ,
                        "description": desc,
                        "accounts_type": _public_accounts_type(desc),
                        "made_up_date": _parse_long_date(desc.split("made up to")[-1]) if "made up to" in desc else None,
                        "pages": int(pg.group(1)) if pg else None,
                        "document_metadata": None,
                        "ixbrl_available": "format=xhtml" in tds[3] or "Download iXBRL" in tds[3],
                        "source": "public_site",
                    }
                )
            if not found and p > 1:
                break
    return out


def _public_accounts_type(desc: str) -> str | None:
    d = desc.lower()
    for key, name in (("group of companies", "group"), ("full accounts", "full"), ("small", "small"), ("micro", "micro-entity"), ("dormant", "dormant"), ("total exemption", "total-exemption-full"), ("audit exemption", "audit-exemption-subsidiary")):
        if key in d:
            return name
    return None


def fetch_url(url: str) -> bytes:
    with httpx.Client(timeout=120, headers={"User-Agent": USER_AGENT}, follow_redirects=True) as c:
        r = c.get(url)
    if r.status_code >= 400:
        raise CompaniesHouseError(f"download failed with HTTP {r.status_code}")
    return r.content


def parse_public_url(url: str) -> dict[str, Any]:
    m = re.search(r"/company/([A-Z0-9]{8})/filing-history/([^/?#]+)/document", url)
    return {"company_number": m.group(1), "transaction_id": m.group(2)} if m else {}
