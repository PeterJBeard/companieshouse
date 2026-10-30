"""MCP tools over stdio and streamable HTTP, plus the highlight viewer routes.

Run:
    python -m ch_accounts_mcp.server                 # stdio (Claude Desktop); viewer on 127.0.0.1:VIEWER_PORT
    python -m ch_accounts_mcp.server --http --port 8000   # streamable HTTP at /mcp, viewer on the same port
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import json
import logging
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, TextContent
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response

from . import __version__
from .cache import get_cache
from .config import override_settings, settings
from .pipeline import PRIMARY, TOOL_VERSION, Document, crop_url, provenance_legend, resolve_statements, viewer_url, with_fresh_urls
from .server_auth import check_bearer, check_signature, sign

log = logging.getLogger("ch_accounts_mcp")

VIEWER_DIR = Path(__file__).resolve().parent.parent / "viewer"
MAX_INLINE_CROPS = 8

INSTRUCTIONS = """\
Reads UK Companies House audited accounts (FRS 102, FRS 101, IFRS), including scanned image-only PDFs.

Rules for answering with this server:
- Every figure and quote comes with a citation_id, a PDF page and the printed page. Always state the page
  (prefer the printed page, as accountants do) and cite the citation_id, for example "£4,812k (p.12) [c_7f3a91aa01bc]".
- Figures whose status is "unverified" (provenance extracted_unverified) did not pass arithmetic checks. Treat them as
  uncertain, say so, and show the crop or ask the user to check the page. Never present an unverified figure as fact.
- Quote text verbatim from get_note, get_audit_report, get_section and search_document; do not paraphrase as a quote.
- Use compute for any ratio or derived figure, so the arithmetic is done here and every input is cited.
- Before giving a final answer that contains numbers or quotes, call check_answer and remove or flag anything unmatched.
- Interpretation that is not in the filing must be labelled as analysis, not from the filing.
"""

mcp = FastMCP("companies-house-accounts", instructions=INSTRUCTIONS)


# --------------------------------------------------------------------------- helpers


def _secrets() -> list[str]:
    s = settings()
    return [x for x in (s.companies_house_api_key, s.anthropic_api_key, s.mcp_bearer_token) if x]


def _scrub(text: str) -> str:
    for sec in _secrets():
        text = text.replace(sec, "[REDACTED]")
    return text


def _json(obj: Any) -> TextContent:
    return TextContent(type="text", text=_scrub(json.dumps(obj, ensure_ascii=False, default=str)))


def _png(data: bytes) -> ImageContent:
    return ImageContent(type="image", data=base64.b64encode(data).decode(), mimeType="image/png")


async def _run(fn, *args, **kwargs):
    t0 = time.perf_counter()
    try:
        res = await anyio.to_thread.run_sync(lambda: fn(*args, **kwargs))
    except Exception as e:  # tool errors go back to the model as JSON, never with secrets
        log.warning("tool error: %s", _scrub(f"{type(e).__name__}: {e}"))
        return {"error": _scrub(f"{type(e).__name__}: {e}"), "timings_ms": {"total": round((time.perf_counter() - t0) * 1000, 1)}, "cache_hit": False}
    if isinstance(res, dict):
        res.setdefault("timings_ms", {})
        res["timings_ms"]["total"] = round((time.perf_counter() - t0) * 1000, 1)
        res.setdefault("cache_hit", False)
    return res


def _doc(doc_id: str) -> Document:
    if not re.fullmatch(r"[0-9a-f]{64}", doc_id or ""):
        raise ValueError("doc_id must be the 64-character SHA-256 returned by load_accounts")
    return Document.open(doc_id)


def _viewer_template(sha: str) -> str:
    sig = sign(sha)
    return f"{settings().public_base_url}/view/{sha}?c={{citation_id}}" + (f"&s={sig}" if sig else "")


def _client():
    from .companies_house import CompaniesHouseClient

    return CompaniesHouseClient(settings().companies_house_api_key)


# --------------------------------------------------------------------------- tool bodies (sync)


def _find_company(query: str) -> dict:
    from .companies_house import public_search

    c = _client()
    try:
        if c.has_key:
            return {"matches": c.search(query), "source": "companies_house_api"}
        return {"matches": public_search(query), "source": "public_site", "note": "COMPANIES_HOUSE_API_KEY is not set, so this used the public search page; company status may be incomplete."}
    finally:
        c.close()


def _list_filings(company_number: str) -> dict:
    from .companies_house import public_accounts_filings

    num = company_number.strip().upper().zfill(8)
    c = _client()
    try:
        filings = c.accounts_filings(num) if c.has_key else public_accounts_filings(num)
    finally:
        c.close()
    cached = {sha: d for d, sha in get_cache().company_docs(num)}
    return {"company_number": num, "filings": filings, "cached_doc_ids": list(cached), "source": "companies_house_api" if c.has_key else "public_site"}


def _load(company_number: str | None, year_end: str | None, transaction_id: str | None, pdf_path: str | None, pdf_url: str | None) -> dict:
    from .companies_house import fetch_url, now_iso, parse_public_url, pick_filing, public_accounts_filings, public_document_url

    t0 = time.perf_counter()
    timings: dict[str, float] = {}
    cache = get_cache()
    num = company_number.strip().upper().zfill(8) if company_number else None
    source: dict[str, Any] = {"retrieved_at": now_iso()}
    doc: Document | None = None
    hit = False
    if sum(bool(x) for x in (pdf_path, pdf_url, transaction_id, year_end)) == 0:
        raise ValueError("give one of: company_number + year_end, transaction_id (with company_number unless already cached), pdf_path, or pdf_url")
    if pdf_path:
        p = Path(pdf_path).expanduser()
        data = p.read_bytes()
        source.update(source="pdf_path", file_name=p.name, company_number=num)
        doc = Document.from_bytes(data, source)
    elif pdf_url:
        info = parse_public_url(pdf_url)
        tx = info.get("transaction_id")
        sha = cache.sha_for_transaction(tx) if tx else None
        if sha and cache.verify_integrity(sha):
            doc, hit = Document.open(sha), True
        else:
            data = fetch_url(pdf_url)
            source.update(source="pdf_url", source_url=pdf_url if "company-information.service.gov.uk" in pdf_url else None, download_url=pdf_url, **info)
            if tx and info.get("company_number"):
                try:
                    c = _client()
                    lst = c.accounts_filings(info["company_number"]) if c.has_key else public_accounts_filings(info["company_number"])
                    c.close()
                    f = next((f for f in lst if f.get("transaction_id") == tx), None)
                    if f:
                        source.update(filing_date=f.get("date"), made_up_date=f.get("made_up_date"), accounts_type=f.get("accounts_type"), filing_description=f.get("description"))
                except Exception:  # filing metadata is a nicety; the document itself is what matters
                    log.info("could not fetch filing metadata for %s", tx)
            doc = Document.from_bytes(data, source)
    else:
        filing = None
        if year_end and not transaction_id:
            if not num:
                raise ValueError("year_end needs company_number")
            c = _client()
            try:
                filing = c.filing_for(num, year_end)
            finally:
                c.close()
            if filing is None:
                raise ValueError(f"no accounts filing made up to {year_end} found for {num}")
            transaction_id = filing["transaction_id"]
        sha = cache.sha_for_transaction(transaction_id)
        if sha and cache.verify_integrity(sha):
            doc, hit = Document.open(sha), True
        else:
            if not num:
                raise ValueError("transaction_id needs company_number when the filing is not cached")
            c = _client()
            try:
                if filing is None:
                    lst = c.accounts_filings(num) if c.has_key else public_accounts_filings(num)
                    filing = next((f for f in lst if f.get("transaction_id") == transaction_id), None)
                t1 = time.perf_counter()
                if c.has_key and filing and filing.get("document_metadata"):
                    data = c.fetch_document(filing["document_metadata"])
                else:
                    data = fetch_url(public_document_url(num, transaction_id))
                timings["download"] = round((time.perf_counter() - t1) * 1000, 1)
            finally:
                c.close()
            source.update(source="companies_house", company_number=num, transaction_id=transaction_id)
            if filing:
                source.update(filing_date=filing.get("date"), made_up_date=filing.get("made_up_date"), accounts_type=filing.get("accounts_type"), filing_description=filing.get("description"))
            doc = Document.from_bytes(data, source)
    pm, pm_hit, t = doc.page_map()
    hit = hit or pm_hit
    timings.update({f"triage_{k}": v for k, v in t.items()})
    m = doc.meta
    if m.get("company_number"):
        cache.add_company_doc(m["company_number"], m.get("made_up_date"), doc.sha)
    statements = [r for r in pm["ranges"] if r["kind"] in PRIMARY]
    return {
        "doc_id": doc.sha,
        "company_number": m.get("company_number"),
        "company_name": m.get("company_name"),
        "company_number_on_document": m.get("company_number_on_document"),
        "made_up_date": m.get("made_up_date"),
        "filing_date": m.get("filing_date"),
        "accounts_type": m.get("accounts_type"),
        "transaction_id": m.get("transaction_id"),
        "source_url": m.get("source_url"),
        "retrieved_at": m.get("retrieved_at"),
        "page_count": pm["page_count"],
        "document_type": "text_layer" if not pm["scanned_pages"] else ("scanned" if not pm["text_layer_pages"] else "mixed"),
        "scanned_pages": len(pm["scanned_pages"]),
        "text_layer_pages": len(pm["text_layer_pages"]),
        "page_ranges": pm["ranges"],
        "primary_statement_pages": statements,
        "notes_index": [{"note": e["note"], "title": e["title"], "pdf_page": e["start_page"]} for e in pm["notes_index"]],
        "notes_index_complete": pm.get("notes_index_complete", False),
        "viewer_url": viewer_url(doc.sha),
        "next": "Call get_primary_statements with this doc_id. Quote page numbers; treat unverified figures as uncertain.",
        "timings_ms": dict(timings, triage_total=round((time.perf_counter() - t0) * 1000, 1)),
        "cache_hit": hit,
    }


def _primary(doc_id: str, statements: list[str] | None, scope: str | None, include_rows: bool) -> tuple[dict, list[bytes]]:
    doc = _doc(doc_id)
    res, hit, timings = doc.primary_statements()
    want = set(resolve_statements(statements))
    sc = scope.lower().strip() if scope else None
    if sc and sc not in ("group", "company"):
        raise ValueError("scope must be group or company")
    sts = [s for s in res["statements"] if s["statement"] in want and (sc is None or s["scope"] == sc or s["scope"] == "unknown")]
    keep_pages = {p for s in sts for p in s["pages"]}
    unverified = [dict(u, crop_url=crop_url(doc.sha, u["citation_id"]), viewer_url=viewer_url(doc.sha, u["citation_id"])) for u in res["unverified"] if u["pdf_page"] in keep_pages and u["statement"] in want]
    out_sts = []
    for s in sts:
        d = dict(s)
        if not include_rows:
            d.pop("rows", None)
        out_sts.append(d)
    crops = doc.crops([u["citation_id"] for u in unverified[:MAX_INLINE_CROPS]])
    m = doc.meta
    out = {
        "doc_id": doc.sha,
        "company_name": m.get("company_name"),
        "company_number": m.get("company_number"),
        "made_up_date": m.get("made_up_date"),
        "source_url": m.get("source_url"),
        "statements": out_sts,
        "verification": {
            "summary": res["verification"]["summary"],
            "cross_checks": [x for x in res["verification"]["cross_checks"]],
            "unverified": unverified,
        },
        "provenance_legend": provenance_legend(),
        "viewer_url_template": _viewer_template(doc.sha),
        "guidance": (
            "Markdown tables mark unverified values with (?). Each value in rows carries a citation_id; call get_citation for the "
            "full citation object and crop. Crops of up to %d unverified figures are attached as images. Quote printed page numbers. "
            "Unverified figures must be presented as uncertain." % MAX_INLINE_CROPS
        ),
        "timings_ms": timings,
        "cache_hit": hit,
    }
    return out, [crops[u["citation_id"]] for u in unverified[:MAX_INLINE_CROPS] if u["citation_id"] in crops]


def _citation(citation_id: str) -> tuple[dict, bytes | None]:
    if not re.fullmatch(r"c_[0-9a-f]{12}", citation_id or ""):
        raise ValueError("citation_id looks like c_ followed by 12 hex characters")
    cache = get_cache()
    sha = cache.sha_for_citation(citation_id)
    if sha is None:
        raise KeyError(f"unknown citation {citation_id}")
    if not cache.verify_integrity(sha):
        return {"error": "the stored PDF no longer matches its hash, so this citation is invalid; reload the filing", "citation_id": citation_id}, None
    doc = Document.open(sha)
    c = doc.citation(citation_id)
    if c is None:
        raise KeyError(f"unknown citation {citation_id}")
    crop = doc.crop(citation_id) if c.get("type") != "computed" else None
    return dict(c, provenance=provenance_legend().get(c["status"])), crop


def _page_image(doc_id: str, page: int, bbox: list[float] | None, dpi: int) -> tuple[dict, bytes]:
    doc = _doc(doc_id)
    n = len(doc.pdf)
    if not 1 <= page <= n:
        raise ValueError(f"page must be between 1 and {n}")
    dpi = max(50, min(int(dpi), 300))
    if bbox is not None and len(bbox) != 4:
        raise ValueError("bbox is [x0, y0, x1, y1] in PDF points")
    pm, _, _ = doc.page_map()
    info = pm["pages"][page - 1]
    data = doc.page_image(page, bbox, dpi)
    return {"doc_id": doc.sha, "pdf_page": page, "printed_page": info.get("printed_page"), "kind": info.get("kind"), "scope": info.get("scope"), "dpi": dpi, "bbox": bbox, "page_size_pt": list(doc.page_size(page)), "viewer_url": viewer_url(doc.sha) + ("&" if "?" in viewer_url(doc.sha) else "?") + f"p={page}"}, data


def _compare_years(company_number: str, years: list[int] | None) -> dict:
    cache = get_cache()
    num = company_number.strip().upper().zfill(8)
    docs = cache.company_docs(num)
    if not docs:
        return {"error": f"no cached filings for {num}; call load_accounts for each year first"}
    spread: dict[tuple, dict] = {}
    by_year: dict[int, str] = {}
    checks = []
    for made_up, sha in docs:
        d = Document.open(sha)
        res, _, _ = d.primary_statements()
        cits = d.citations()
        yr = int(made_up[:4]) if made_up else None
        if years and yr not in years:
            continue
        by_year[yr] = sha
        for s in res["statements"]:
            for r in s.get("rows", []):
                for k, v in (r.get("values") or {}).items():
                    c = cits.get(v["citation_id"], {})
                    fy = c.get("year")
                    key = (s["statement"], s["scope"], re.sub(r"\s+", " ", r["label"].lower()).strip())
                    e = spread.setdefault(key, {"statement": s["statement"], "scope": s["scope"], "label": r["label"], "values": {}})
                    slot = e["values"].setdefault(str(fy), [])
                    slot.append({"value": v["value"], "raw": v["raw"], "status": v["status"], "citation_id": v["citation_id"], "from_filing_year": yr, "period": c.get("period")})
    for e in spread.values():
        for fy, vals in e["values"].items():
            cur = [v for v in vals if v["period"] == "current"]
            pri = [v for v in vals if v["period"] == "prior"]
            for a in cur:
                for b in pri:
                    if b["from_filing_year"] != a["from_filing_year"]:
                        ok = a["value"] == b["value"]
                        checks.append({"label": e["label"], "statement": e["statement"], "scope": e["scope"], "year": fy, "current_as_filed": a["citation_id"], "comparative_in_later_filing": b["citation_id"], "agrees": ok, "note": None if ok else "comparative differs: possible restatement or misread; check both pages"})
    return {"company_number": num, "filings": {str(k): v for k, v in by_year.items()}, "lines": list(spread.values()), "prior_year_agreement": checks}


# --------------------------------------------------------------------------- MCP tools


@mcp.tool(description="Search Companies House for a company by name or number. Returns company number, name, status and incorporation date.", structured_output=False)
async def find_company(query: str) -> list:
    return [_json(await _run(_find_company, query))]


@mcp.tool(description="List a company's accounts filings with made-up date, accounts type, page count and transaction id. Use the transaction_id (or year_end) with load_accounts.", structured_output=False)
async def list_accounts_filings(company_number: str) -> list:
    return [_json(await _run(_list_filings, company_number))]


@mcp.tool(
    description=(
        "Load one set of audited accounts and triage it. Give exactly one of: company_number + year_end (YYYY or YYYY-MM-DD), "
        "transaction_id (with company_number unless cached), pdf_path, or pdf_url. Returns doc_id, page count, whether pages are "
        "scanned, the page map (statement kind, group/company scope, page ranges) and notes index. Scanned filings are OCR'd; "
        "first load can take tens of seconds on CPU, later calls are cached."
    ),
    structured_output=False,
)
async def load_accounts(company_number: str | None = None, year_end: str | None = None, transaction_id: str | None = None, pdf_path: str | None = None, pdf_url: str | None = None) -> list:
    return [_json(await _run(_load, company_number, year_end, transaction_id, pdf_path, pdf_url))]


@mcp.tool(
    description=(
        "Primary statements (income statement, balance sheet, cash flow, changes in equity) as markdown tables and JSON rows, "
        "each value with a citation_id, its verification status and PDF/printed page. statements: any of income, balance, "
        "cash_flow, equity. scope: group or company. Values marked unverified (shown '(?)') failed arithmetic checks: treat "
        "them as uncertain and say so. Always quote page numbers. Crops of unverified figures are attached."
    ),
    structured_output=False,
)
async def get_primary_statements(doc_id: str, statements: list[str] | None = None, scope: str | None = None, include_rows: bool = True) -> list:
    res = await _run(_primary, doc_id, statements, scope, include_rows)
    if isinstance(res, dict):
        return [_json(res)]
    data, crops = res
    return [_json(data)] + [_png(c) for c in crops]


@mcp.tool(description="Full citation object for a citation_id: document hash, Companies House link, PDF and printed page, verbatim text, highlight spans in PDF points, checks, provenance, plus an image crop of the source row and a viewer deep link.", structured_output=False)
async def get_citation(citation_id: str) -> list:
    res = await _run(_citation, citation_id)
    if isinstance(res, dict):
        return [_json(res)]
    c, crop = res
    return [_json(c)] + ([_png(crop)] if crop else [])


@mcp.tool(description="Render a page (or a bbox [x0,y0,x1,y1] in PDF points) as PNG so you can inspect it, for example to check an unverified figure. page is the 1-based PDF page index.", structured_output=False)
async def get_page_image(doc_id: str, page: int, bbox: list[float] | None = None, dpi: int = 150) -> list:
    res = await _run(_page_image, doc_id, page, bbox, dpi)
    if isinstance(res, dict):
        return [_json(res)]
    meta, png = res
    return [_json(meta), _png(png)]


@mcp.tool(description="A note to the financial statements by number or topic (for example 'borrowings', 'leases', 'related party', '12'). Returns verbatim text with pages and citation ids, and any numeric tables cast for verification. Quote, do not paraphrase.", structured_output=False)
async def get_note(doc_id: str, topic: str) -> list:
    from .sections import get_note as fn

    return [_json(await _run(lambda: fn(_doc(doc_id), topic)))]


@mcp.tool(description="The independent auditor's report: firm, senior statutory auditor, opinion type (unmodified, qualified, adverse, disclaimer), emphasis of matter, material uncertainty related to going concern, key audit matters, and the full verbatim text with pages. Each field cites the paragraph it came from; quote it.", structured_output=False)
async def get_audit_report(doc_id: str) -> list:
    from .sections import get_audit_report as fn

    return [_json(await _run(lambda: fn(_doc(doc_id))))]


@mcp.tool(description="Verbatim text of a section with pages: strategic_report, directors_report, accounting_policies, going_concern or post_balance_sheet_events.", structured_output=False)
async def get_section(doc_id: str, section: str) -> list:
    from .sections import get_section as fn

    return [_json(await _run(lambda: fn(_doc(doc_id), section)))]


@mcp.tool(description="Ranked verbatim snippets matching a query, each with page numbers and a citation id. The first search of a scanned filing OCRs every page (slow on CPU); later searches are fast.", structured_output=False)
async def search_document(doc_id: str, query: str, limit: int = 10) -> list:
    from .sections import search_document as fn

    return [_json(await _run(lambda: fn(_doc(doc_id), query, max(1, min(limit, 30)))))]


@mcp.tool(description="Primary statements spread across years from filings already loaded for this company, with checks that each year's comparatives agree to the prior year's filing.", structured_output=False)
async def compare_years(company_number: str, years: list[int] | None = None) -> list:
    return [_json(await _run(_compare_years, company_number, years))]


@mcp.tool(
    description=(
        "Check a draft answer before sending it. Every number, percentage and quoted passage is matched to a citation or computed "
        "result (allowing unit conversion such as 4,812 £'000 = £4.8m, and rounding) and tagged matched or UNMATCHED. Call this "
        "before every final answer and remove or flag anything unmatched."
    ),
    structured_output=False,
)
async def check_answer(doc_id: str, draft_text: str) -> list:
    from .citations import check_answer as fn

    return [_json(await _run(lambda: fn(_doc(doc_id), draft_text)))]


@mcp.tool(
    description=(
        "Compute a metric on the server with every input cited and the formula shown: net_assets, gross_margin, operating_margin, "
        "net_margin, current_ratio, net_debt, ebitda, interest_cover, gearing, revenue_growth. Or pass definition as an "
        "expression over citation ids, for example 'c_aaa + c_bbb - c_ccc'. State the definition used in your answer."
    ),
    structured_output=False,
)
async def compute(doc_id: str, metric: str, definition: str | None = None, scope: str | None = None, year: int | None = None) -> list:
    from .compute import compute as fn

    return [_json(await _run(lambda: fn(_doc(doc_id), metric, definition, scope, year)))]


@mcp.tool(description="Export a tick-and-reference workpaper (PDF and Excel) for a list of citation_ids or an answer text: header with filing details and document hash, one row per figure with crop, provenance and checks, unverified and unmatched items listed separately, and a reviewer sign-off block.", structured_output=False)
async def export_workpaper(doc_id: str, citation_ids: list[str] | None = None, answer: str | None = None, reviewer: str | None = None) -> list:
    from .workpaper import export_workpaper as fn

    return [_json(await _run(lambda: fn(_doc(doc_id), citation_ids, answer, reviewer)))]


# --------------------------------------------------------------------------- viewer routes


def _authorised(request: Request, sha: str) -> bool:
    return check_signature(sha, request.query_params.get("s")) or (bool(settings().mcp_bearer_token) and check_bearer(request.headers.get("authorization")))


def _deny() -> Response:
    return JSONResponse({"error": "missing or invalid link signature"}, status_code=403)


def _sha_ok(sha: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{64}", sha))


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request: Request) -> Response:
    return JSONResponse({"ok": True, "version": __version__})


@mcp.custom_route("/view/{sha}", methods=["GET"])
async def view(request: Request) -> Response:
    sha = request.path_params["sha"]
    if not _sha_ok(sha) or not _authorised(request, sha):
        return _deny()
    html = (VIEWER_DIR / "index.html").read_text(encoding="utf-8")
    cfg = {"sha": sha, "sig": request.query_params.get("s") or "", "citation": request.query_params.get("c") or "", "page": request.query_params.get("p") or "", "trail": request.query_params.get("trail") == "1"}
    return HTMLResponse(html.replace("/*__CONFIG__*/null", json.dumps(cfg)))


@mcp.custom_route("/viewer/{name}", methods=["GET"])
async def viewer_static(request: Request) -> Response:
    name = request.path_params["name"]
    if not re.fullmatch(r"[a-z0-9_\-]+\.(js|css)", name):
        return Response(status_code=404)
    path = VIEWER_DIR / name
    return FileResponse(path) if path.exists() else Response(status_code=404)


@mcp.custom_route("/api/doc/{sha}", methods=["GET"])
async def api_doc(request: Request) -> Response:
    sha = request.path_params["sha"]
    if not _sha_ok(sha) or not _authorised(request, sha):
        return _deny()

    def body() -> dict:
        d = Document.open(sha)
        pm, _, _ = d.page_map()
        sizes = [list(d.page_size(p)) for p in range(1, pm["page_count"] + 1)]
        m = d.meta
        return {
            "doc_id": sha,
            "company_name": m.get("company_name"),
            "company_number": m.get("company_number"),
            "made_up_date": m.get("made_up_date"),
            "accounts_type": m.get("accounts_type"),
            "source_url": m.get("source_url"),
            "transaction_id": m.get("transaction_id"),
            "page_count": pm["page_count"],
            "page_sizes": sizes,
            "pages": [{"page": p["page"], "kind": p["kind"], "scope": p["scope"], "printed_page": p.get("printed_page")} for p in pm["pages"]],
            "legend": provenance_legend(),
        }

    try:
        return JSONResponse(await anyio.to_thread.run_sync(body))
    except KeyError:
        return JSONResponse({"error": "unknown document"}, status_code=404)


@mcp.custom_route("/api/citations/{sha}", methods=["GET"])
async def api_citations(request: Request) -> Response:
    sha = request.path_params["sha"]
    if not _sha_ok(sha) or not _authorised(request, sha):
        return _deny()
    try:
        d = Document.open(sha)
    except KeyError:
        return JSONResponse({"error": "unknown document"}, status_code=404)
    cits = await anyio.to_thread.run_sync(d.citations)
    return JSONResponse({"citations": [with_fresh_urls(c) for c in cits.values()]})


@mcp.custom_route("/tile/{sha}/{page}.png", methods=["GET"])
async def tile(request: Request) -> Response:
    sha = request.path_params["sha"]
    if not _sha_ok(sha) or not _authorised(request, sha):
        return _deny()
    try:
        page = int(request.path_params["page"])
        d = Document.open(sha)
        if not 1 <= page <= len(d.pdf):
            return Response(status_code=404)
        data = await anyio.to_thread.run_sync(lambda: d.tile(page))
    except (KeyError, ValueError):
        return Response(status_code=404)
    return Response(data, media_type="image/png", headers={"Cache-Control": "private, max-age=86400"})


@mcp.custom_route("/crop/{sha}/{cid}.png", methods=["GET"])
async def crop(request: Request) -> Response:
    sha, cid = request.path_params["sha"], request.path_params["cid"]
    if not _sha_ok(sha) or not _authorised(request, sha) or not re.fullmatch(r"c_[0-9a-f]{12}", cid):
        return _deny()
    try:
        d = Document.open(sha)
        data = await anyio.to_thread.run_sync(lambda: d.crop(cid))
    except KeyError:
        return Response(status_code=404)
    if data is None:
        return Response(status_code=404)
    return Response(data, media_type="image/png", headers={"Cache-Control": "private, max-age=86400"})


@mcp.custom_route("/workpaper/{sha}/{name}", methods=["GET"])
async def workpaper_file(request: Request) -> Response:
    sha, name = request.path_params["sha"], request.path_params["name"]
    if not _sha_ok(sha) or not _authorised(request, sha) or not re.fullmatch(r"workpaper_\d{8}T\d{6}Z\.(pdf|xlsx)", name):
        return _deny()
    path = get_cache().doc_dir(sha) / "workpapers" / name
    if not path.exists():
        return Response(status_code=404)
    return FileResponse(path, filename=name)


# --------------------------------------------------------------------------- entry points


class BearerAuthMiddleware:
    """Requires 'Authorization: Bearer <MCP_BEARER_TOKEN>' on the MCP endpoint. Viewer links use signatures instead."""

    def __init__(self, app, protected_prefix: str = "/mcp"):
        self.app = app
        self.prefix = protected_prefix

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith(self.prefix):
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            if not check_bearer(headers.get("authorization")):
                resp = JSONResponse({"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
                await resp(scope, receive, send)
                return
        await self.app(scope, receive, send)


def viewer_app():
    """Starlette app with only the viewer routes (used alongside the stdio transport)."""
    from starlette.applications import Starlette

    return Starlette(routes=list(mcp._custom_starlette_routes))


def _start_viewer_thread(port: int) -> None:
    import uvicorn

    def serve() -> None:
        try:
            uvicorn.run(viewer_app(), host="127.0.0.1", port=port, log_level="warning", access_log=False)
        except SystemExit:
            log.warning("viewer could not bind 127.0.0.1:%s (already in use?)", port)
        except Exception:
            log.exception("viewer stopped")

    threading.Thread(target=serve, name="viewer", daemon=True).start()


def http_app(host: str, port: int):
    from mcp.server.transport_security import TransportSecuritySettings

    allowed = [h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    if allowed:
        mcp.settings.transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=allowed, allowed_origins=[f"https://{h}" for h in allowed] + [f"http://{h}" for h in allowed])
    elif host not in ("127.0.0.1", "localhost"):
        # behind a proxy the Host header is the public name; bearer auth protects the endpoint
        mcp.settings.transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    return BearerAuthMiddleware(mcp.streamable_http_app(), mcp.settings.streamable_http_path)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="ch-accounts-mcp", description="Companies House audited accounts MCP server")
    ap.add_argument("--http", action="store_true", help="serve streamable HTTP instead of stdio")
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    ap.add_argument("--allow-no-auth", action="store_true", help="allow HTTP on a non-loopback host without MCP_BEARER_TOKEN (not recommended)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    s = settings()
    if args.http:
        if not os.environ.get("PUBLIC_BASE_URL"):
            override_settings(dataclasses.replace(s, public_base_url=f"http://{args.host if args.host != '0.0.0.0' else '127.0.0.1'}:{args.port}"))
        if not settings().mcp_bearer_token and args.host not in ("127.0.0.1", "localhost") and not args.allow_no_auth:
            sys.exit("refusing to serve HTTP on a non-loopback host without MCP_BEARER_TOKEN (use --allow-no-auth to override)")
        log.info("starting %s over streamable HTTP on %s:%s; settings %s", TOOL_VERSION, args.host, args.port, settings().redacted())
        import uvicorn

        uvicorn.run(http_app(args.host, args.port), host=args.host, port=args.port, log_level="info")
        return
    log.info("starting %s over stdio; settings %s", TOOL_VERSION, s.redacted())
    if s.start_viewer:
        _start_viewer_thread(s.viewer_port)
    mcp.run("stdio")


if __name__ == "__main__":
    main()
