"""Document object tying fetch, triage, OCR, layout, verification and citations together."""

from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from decimal import Decimal
from typing import Any

import pymupdf

from . import __version__, ocr
from .cache import Cache, get_cache
from .citations import PROVENANCE, build_figure_citation, build_quote_citation, citation_id, find_quote, render_crop
from .config import settings
from .layout import PageLayout, build_rows, insert_dash, missing_value_slots, rebuild_page, to_markdown
from .triage import (
    PageInfo,
    build_notes_index,
    build_page_map,
    classify_page,
    header_page_number,
    note_heading_candidates,
    page_ranges,
    printed_page_number,
    reconcile_printed_pages,
)
from .verify import VERIFIED, Figure, cast_all, cast_rows, cross_check, figures_from_layout, summary

log = logging.getLogger(__name__)

TOOL_VERSION = f"ch_accounts_mcp {__version__}"
PRIMARY = ("income_statement", "balance_sheet", "equity_statement", "cash_flow")
STATEMENT_ALIASES = {
    "income": "income_statement",
    "income_statement": "income_statement",
    "profit_and_loss": "income_statement",
    "pnl": "income_statement",
    "balance": "balance_sheet",
    "balance_sheet": "balance_sheet",
    "financial_position": "balance_sheet",
    "cash_flow": "cash_flow",
    "cashflow": "cash_flow",
    "equity": "equity_statement",
    "equity_statement": "equity_statement",
    "changes_in_equity": "equity_statement",
}
STATEMENT_TITLES = {
    "income_statement": "Income statement / profit and loss account",
    "balance_sheet": "Balance sheet / statement of financial position",
    "equity_statement": "Statement of changes in equity",
    "cash_flow": "Statement of cash flows",
}
TILE_DPI = 110
RESULT_VERSION = 8
PAGEMAP_VERSION = 3

_doc_locks: dict[str, threading.RLock] = {}
_doc_locks_guard = threading.Lock()
_open_docs: dict[str, "Document"] = {}


def _lock_for(sha: str) -> threading.RLock:
    with _doc_locks_guard:
        return _doc_locks.setdefault(sha, threading.RLock())


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 1)


def viewer_url(sha: str, cid: str | None = None) -> str:
    from .server_auth import sign

    base = settings().public_base_url
    q = []
    if cid:
        q.append(f"c={cid}")
    sig = sign(sha)
    if sig:
        q.append(f"s={sig}")
    return f"{base}/view/{sha}" + ("?" + "&".join(q) if q else "")


def with_fresh_urls(c: dict) -> dict:
    """Links depend on PUBLIC_BASE_URL and the signing key at serve time, not when the citation was made."""
    c = dict(c)
    sha, cid = c["doc_sha256"], c["citation_id"]
    if c.get("type") == "computed":
        first = (c.get("inputs") or [{}])[0].get("citation_id")
        c["viewer_url"] = viewer_url(sha, first) if first else None
    else:
        c["viewer_url"] = viewer_url(sha, cid)
        c["crop_url"] = crop_url(sha, cid)
    return c


def crop_url(sha: str, cid: str) -> str:
    from .server_auth import sign

    sig = sign(sha)
    return f"{settings().public_base_url}/crop/{sha}/{cid}.png" + (f"?s={sig}" if sig else "")


class Document:
    def __init__(self, sha: str, cache: Cache):
        self.sha = sha
        self.cache = cache
        self.meta: dict = cache.get_json(sha, "meta.json") or {"sha256": sha}
        self._pdf: pymupdf.Document | None = None
        self._pdf_lock = threading.RLock()
        self._layouts: dict[tuple[int, int], PageLayout] = {}

    # ------------------------------------------------------------------ construction

    @classmethod
    def from_bytes(cls, data: bytes, source: dict, cache: Cache | None = None) -> "Document":
        cache = cache or get_cache()
        if not data.startswith(b"%PDF"):
            raise ValueError("the downloaded file is not a PDF")
        sha = cache.store_pdf(data)
        doc = cls.open(sha, cache)
        meta = dict(doc.meta)
        for k, v in source.items():
            if v is not None and not meta.get(k):
                meta[k] = v
        meta.setdefault("retrieved_at", source.get("retrieved_at"))
        meta["sha256"] = sha
        meta["size_bytes"] = len(data)
        meta["tool_version"] = TOOL_VERSION
        if meta.get("company_number") and meta.get("transaction_id") and not meta.get("source_url"):
            from .companies_house import public_document_url

            meta["source_url"] = public_document_url(meta["company_number"], meta["transaction_id"])
        doc.meta = meta
        cache.put_json(sha, "meta.json", meta)
        if meta.get("transaction_id"):
            cache.map_transaction(meta["transaction_id"], sha)
        return doc

    @classmethod
    def open(cls, sha: str, cache: Cache | None = None) -> "Document":
        cache = cache or get_cache()
        if sha in _open_docs:
            return _open_docs[sha]
        if not cache.has_doc(sha):
            raise KeyError(f"unknown doc_id {sha}; call load_accounts first")
        d = cls(sha, cache)
        _open_docs[sha] = d
        return d

    @property
    def pdf(self) -> pymupdf.Document:
        with self._pdf_lock:
            if self._pdf is None:
                self._pdf = pymupdf.open(str(self.cache.pdf_path(self.sha)))
            return self._pdf

    @property
    def path(self) -> str:
        return str(self.cache.pdf_path(self.sha))

    def page_size(self, page: int) -> tuple[float, float]:
        with self._pdf_lock:
            if self._pdf is None:
                self._pdf = pymupdf.open(self.path)
            r = self._pdf[page - 1].rect
        return r.width, r.height

    def save_meta(self) -> None:
        self.cache.put_json(self.sha, "meta.json", self.meta)

    # ------------------------------------------------------------------ triage

    def page_map(self) -> tuple[dict, bool, dict]:
        cached = self.cache.get_json(self.sha, "pagemap.json")
        if cached and cached.get("pagemap_version") == PAGEMAP_VERSION:
            return cached, True, {}
        with _lock_for(self.sha):
            cached = self.cache.get_json(self.sha, "pagemap.json")
            if cached and cached.get("pagemap_version") == PAGEMAP_VERSION:
                return cached, True, {}
            return self._triage()

    def _triage(self) -> tuple[dict, bool, dict]:
        timings: dict[str, float] = {}
        t0 = time.perf_counter()
        n = len(self.pdf)
        infos: list[PageInfo] = []
        scanned: list[int] = []
        text_tokens: dict[int, list[dict]] = {}
        with self._pdf_lock:
            for i in range(n):
                toks = ocr.text_layer_tokens(self.pdf[i])
                if toks:
                    text_tokens[i + 1] = toks
                else:
                    scanned.append(i)
        for p, toks in text_tokens.items():
            self.cache.put_json(self.sha, f"pages/{p}.tokens.text.json", toks)
        timings["text_layer_check"] = _ms(t0)
        t1 = time.perf_counter()
        futs = ocr.get_pool().triage_pages(self.path, scanned) if scanned else {}
        triage_tokens: dict[int, dict] = {}
        for i, fut in futs.items():
            res = fut.result()
            triage_tokens[i + 1] = res
            self.cache.put_json(self.sha, f"pages/{i + 1}.triage.json", res)
        timings["triage_ocr"] = _ms(t1)
        t2 = time.perf_counter()
        cands = []
        for p in range(1, n + 1):
            w, h = self.page_size(p)
            if p in text_tokens:
                toks = text_tokens[p]
                bottom = [t for t in toks if t["b"][1] > h * (1 - ocr.TRIAGE_BOTTOM_FRAC)]
                info = PageInfo(page=p, text_layer=True)
            else:
                tr = triage_tokens[p]
                toks, bottom = tr["tokens"], tr["bottom_tokens"]
                info = PageInfo(page=p, triage_ms=tr["ms"])
            c = classify_page(toks, h)
            if c:
                info.kind, info.scope, info.heading = c
            info.printed_page = printed_page_number(bottom, w) or header_page_number(toks, h)
            infos.append(info)
            cands.append((p, toks, w))
        build_page_map(infos)
        reconcile_printed_pages(infos)
        notes_cands = []
        for (p, toks, w), info in zip(cands, infos):
            if info.kind == "notes":
                notes_cands += note_heading_candidates(p, toks, w)
        self._detect_identity(infos, {p: t for p, t, _ in cands})
        notes_index = build_notes_index(notes_cands)
        _close_note_ranges(notes_index, infos)
        timings["classify"] = _ms(t2)
        pm = {
            "pagemap_version": PAGEMAP_VERSION,
            "pages": [i.to_dict() for i in infos],
            "ranges": page_ranges(infos),
            "notes_index": notes_index,
            "notes_index_complete": not scanned,
            "text_layer_pages": sorted(text_tokens),
            "scanned_pages": [i + 1 for i in scanned],
            "page_count": n,
            "triage_timings_ms": timings,
        }
        self.cache.put_json(self.sha, "pagemap.json", pm)
        self.cache.append_log(self.sha, {"at": time.time(), "event": "triage", "tool_version": TOOL_VERSION, "ocr": settings().ocr_engine, "timings_ms": timings})
        threading.Thread(target=self.prerender_tiles, daemon=True).start()
        return pm, False, timings

    def _detect_identity(self, infos: list[PageInfo], toks: dict[int, list[dict]]) -> None:
        from .triage import merge_visual_lines

        changed = False
        for info in infos[:60]:
            lines = [t for _, t in merge_visual_lines(toks.get(info.page, []))]
            for ln in lines:
                if not self.meta.get("company_number_on_document"):
                    m = re.search(r"(?:registered|registration|company)\s*(?:number|no\.?)\s*[:.]?\s*([A-Z]{0,2}\d{6,8})", ln, re.I)
                    if m:
                        self.meta["company_number_on_document"] = m.group(1).zfill(8) if m.group(1).isdigit() else m.group(1)
                        changed = True
                if not self.meta.get("company_name_on_document") and info.kind in ("front", "balance_sheet", "income_statement", "unknown", "directors_report", "strategic_report"):
                    if re.search(r"\b(limited|ltd|plc|llp|p\.l\.c\.)\.?$", ln.strip(), re.I) and len(ln) < 90 and not re.search(r"report|statement|for the year", ln, re.I):
                        self.meta["company_name_on_document"] = ln.strip()
                        changed = True
            if info.kind == "balance_sheet" and not self.meta.get("balance_sheet_date_on_document") and info.heading:
                m = re.search(r"(\d{1,2}\s+[A-Za-z]+\s+\d{4})", info.heading)
                if m:
                    from .companies_house import _parse_long_date

                    self.meta["balance_sheet_date_on_document"] = _parse_long_date(m.group(1))
                    changed = True
        if not self.meta.get("company_number") and self.meta.get("company_number_on_document"):
            self.meta["company_number"] = self.meta["company_number_on_document"]
            changed = True
        if not self.meta.get("company_name") and self.meta.get("company_name_on_document"):
            self.meta["company_name"] = self.meta["company_name_on_document"]
            changed = True
        if not self.meta.get("made_up_date") and self.meta.get("balance_sheet_date_on_document"):
            self.meta["made_up_date"] = self.meta["balance_sheet_date_on_document"]
            changed = True
        if changed:
            self.save_meta()

    def prerender_tiles(self) -> None:
        try:
            for p in range(1, len(self.pdf) + 1):
                self.tile(p)
        except Exception:  # pragma: no cover - best effort
            log.exception("tile pre-render failed")

    def tile(self, page: int) -> bytes:
        name = f"tiles/{page}.png"
        data = self.cache.get_bytes(self.sha, name)
        if data is None:
            with self._pdf_lock:
                if self._pdf is None:
                    self._pdf = pymupdf.open(self.path)
                data = ocr.render_png(self._pdf[page - 1], TILE_DPI)
            self.cache.put_bytes(self.sha, name, data)
        return data

    def page_image(self, page: int, bbox: list[float] | None = None, dpi: int = 150) -> bytes:
        with self._pdf_lock:
            pg = self.pdf[page - 1]
            clip = pymupdf.Rect(bbox) & pg.rect if bbox else None
            return ocr.render_png(pg, dpi, clip)

    # ------------------------------------------------------------------ tokens and layout

    def tokens(self, pages: list[int], dpi: int) -> tuple[dict[int, list[dict]], dict[str, float]]:
        pm, _, _ = self.page_map()
        text_pages = set(pm["text_layer_pages"])
        out: dict[int, list[dict]] = {}
        todo = []
        t0 = time.perf_counter()
        for p in pages:
            if p in text_pages:
                out[p] = self.cache.get_json(self.sha, f"pages/{p}.tokens.text.json") or []
                continue
            cached = self.cache.get_json(self.sha, f"pages/{p}.tokens.{dpi}.json")
            if cached is not None:
                out[p] = cached["tokens"]
            else:
                todo.append(p)
        if todo:
            futs = ocr.get_pool().ocr_pages(self.path, [p - 1 for p in todo], dpi)
            for i, fut in futs.items():
                res = fut.result()
                self.cache.put_json(self.sha, f"pages/{i + 1}.tokens.{dpi}.json", res)
                out[i + 1] = res["tokens"]
                if res.get("engine") and self.meta.get("ocr_model") != res["engine"]:
                    self.meta["ocr_model"] = res["engine"]
                    self.save_meta()
        return out, {f"ocr_{dpi}dpi": _ms(t0), f"ocr_{dpi}dpi_pages": len(todo)}

    def is_text_page(self, page: int) -> bool:
        pm, _, _ = self.page_map()
        return page in set(pm["text_layer_pages"])

    def layout(self, page: int, tokens: list[dict], dpi: int) -> PageLayout:
        key = (page, dpi)
        if key not in self._layouts:
            w, h = self.page_size(page)
            lay = rebuild_page(tokens, page, w, h)
            if not self.is_text_page(page):
                self._fill_ink_dashes(lay)
            self._layouts[key] = lay
        return self._layouts[key]

    def _fill_ink_dashes(self, lay: PageLayout) -> None:
        slots = missing_value_slots(lay)
        if not slots:
            return
        cols_by_row = {id(r): s.value_columns() for s in lay.sections for r in s.rows}
        with self._pdf_lock:
            gray = ocr.render_gray(self.pdf[lay.page - 1], ocr.DASH_DPI)
        found: dict[int, list] = {}
        for row, col, rect in slots:
            bbox = ocr.flat_dash_in(gray, rect)
            # a dash sits on the text centre line; rule fragments sit at the row's top or bottom
            if bbox and abs((bbox[1] + bbox[3]) / 2 - (rect[1] + rect[3]) / 2) <= 0.3 * (rect[3] - rect[1]):
                found.setdefault(id(row), []).append((row, col, bbox))
        for hits in found.values():
            row = hits[0][0]
            cols = cols_by_row[id(row)]
            # a line with no values read is a heading unless every column shows a dash
            if not row.values and len(hits) < len({c.key for c in cols}):
                continue
            for _, col, bbox in hits:
                insert_dash(row, col, bbox, cols)

    # ------------------------------------------------------------------ primary statements

    def primary_statements(self) -> tuple[dict, bool, dict]:
        cached = self.cache.get_json(self.sha, "primary.json")
        if cached and cached.get("result_version") == RESULT_VERSION:
            return cached, True, {}
        with _lock_for(self.sha):
            cached = self.cache.get_json(self.sha, "primary.json")
            if cached and cached.get("result_version") == RESULT_VERSION:
                return cached, True, {}
            return self._extract_primary()

    def _extract_primary(self) -> tuple[dict, bool, dict]:
        timings: dict[str, float] = {}
        pm, _, _ = self.page_map()
        infos = {p["page"]: p for p in pm["pages"]}
        pages = [p["page"] for p in pm["pages"] if p["kind"] in PRIMARY]
        toks, t = self.tokens(pages, ocr.FULL_DPI)
        timings.update(t)
        t0 = time.perf_counter()
        layouts_a = {p: self.layout(p, toks[p], ocr.FULL_DPI) for p in pages}
        timings["layout"] = _ms(t0)
        t0 = time.perf_counter()
        figs_a, secs_a = self._figures(layouts_a, infos, "a")
        self._verify(figs_a, secs_a)
        timings["verify"] = _ms(t0)

        reread_pages = sorted({f.page for f in figs_a if not f.verified and not self.is_text_page(f.page)})
        rounding_confirmed: set[str] = set()
        final = figs_a
        if reread_pages:
            toks_b, t = self.tokens(reread_pages, ocr.REREAD_DPI)
            timings["reread_" + list(t)[0]] = list(t.values())[0]
            timings["reread_pages"] = len(reread_pages)
            t0 = time.perf_counter()
            layouts_b = dict(layouts_a)
            for p in reread_pages:
                layouts_b[p] = self.layout(p, toks_b[p], ocr.REREAD_DPI)
            figs_b, secs_b = self._figures(layouts_b, infos, "b")
            self._verify(figs_b, secs_b)
            final, rounding_confirmed = _merge_reread(figs_a, figs_b, set(reread_pages))
            timings["reread_verify"] = _ms(t0)
        for f in final:
            if f.status == "unverified" and f.provisional_rounding and f.fid in rounding_confirmed:
                f.status = "verified_rounding"
        t0 = time.perf_counter()
        result = self._assemble(final, layouts_a, infos, pm)
        timings["citations"] = _ms(t0)
        result["extraction_timings_ms"] = timings
        self.cache.put_json(self.sha, "primary.json", result)
        self.cache.append_log(
            self.sha,
            {"at": time.time(), "event": "primary_statements", "tool_version": TOOL_VERSION, "ocr_model": self.meta.get("ocr_model"), "summary": result["verification"]["summary"], "timings_ms": timings},
        )
        return result, False, timings

    def _figures(self, layouts: dict[int, PageLayout], infos: dict[int, dict], prefix: str) -> tuple[list[Figure], dict]:
        figs: list[Figure] = []
        secs = {}
        for p, L in layouts.items():
            info = infos[p]
            figs += figures_from_layout(L, info["kind"], info["scope"], prefix)
            for si, s in enumerate(L.sections):
                secs[(p, si)] = s
        return figs, secs

    @staticmethod
    def _verify(figs: list[Figure], secs: dict) -> list[dict]:
        cast_all(figs)
        cast_rows(figs, secs)
        return cross_check(figs)

    def _assemble(self, figs: list[Figure], layouts: dict[int, PageLayout], infos: dict[int, dict], pm: dict) -> dict:
        sha = self.sha
        doc = dict(self.meta)
        doc["sha256"] = sha
        fid_to_cid = {f.fid: citation_id(sha, f.page, f.key, f.row.y0.__round__(1), f.cell.bbox[2].__round__(1), f.raw) for f in figs}
        citations = {}
        for f in figs:
            c = build_figure_citation(f, doc, infos[f.page], fid_to_cid)
            c["crop_url"] = crop_url(sha, c["citation_id"])
            c["viewer_url"] = viewer_url(sha, c["citation_id"])
            citations[c["citation_id"]] = c
        self._store_citations(citations)
        statements = []
        groups: dict[tuple[str, str], list[Figure]] = {}
        for f in figs:
            groups.setdefault((f.statement, f.scope), []).append(f)
        order = {k: i for i, k in enumerate(PRIMARY)}
        for (st, scope), fl in sorted(groups.items(), key=lambda kv: (order.get(kv[0][0], 9), kv[0][1])):
            pages = sorted({f.page for f in fl})
            by_cell = {id(f.cell): f for f in fl}
            rows_out = []
            cols: dict[str, dict] = {}
            md_parts = []
            for p in pages:
                L = layouts[p]
                statuses = {id(f.cell): f.status for f in fl if f.page == p}
                info = infos[p]
                md_parts.append(f"#### {info.get('heading') or STATEMENT_TITLES[st]} (PDF page {p}" + (f", printed page {info['printed_page']}" if info.get("printed_page") else "") + ")")
                md_parts.append(to_markdown(L, statuses))
                for si, s in enumerate(L.sections):
                    for c in s.value_columns():
                        if c.scope and c.scope != scope and scope != "unknown":
                            continue
                        cols.setdefault(c.key, {"key": c.key, "year": c.year, "period": c.period, "scope": c.scope or scope, "unit": c.unit, "restated": c.restated, "header": " ".join(h.text for h in c.header_cells)})
                    for r in s.rows:
                        if r.kind == "consumed":
                            continue
                        vals = {}
                        for k, cell in r.values.items():
                            f = by_cell.get(id(cell))
                            if f is None:
                                continue
                            cid = fid_to_cid[f.fid]
                            vals[k] = {"value": float(f.value), "raw": f.raw, "status": f.status, "provenance": citations[cid]["status"], "citation_id": cid}
                        if not vals and not r.label:
                            continue
                        if not vals and len(r.label) > 120:
                            continue
                        rows_out.append({"label": r.label, "note": r.note, "page": p, "y": round(r.y0, 1), "values": vals} if vals else {"label": r.label, "page": p, "y": round(r.y0, 1), "heading": True})
            units = sorted({f.unit for f in fl if f.unit})
            statements.append(
                {
                    "statement": st,
                    "scope": scope,
                    "title": STATEMENT_TITLES.get(st, st),
                    "pages": pages,
                    "printed_pages": [infos[p].get("printed_page") for p in pages],
                    "unit": units[0] if len(units) == 1 else units,
                    "columns": list(cols.values()),
                    "markdown": "\n\n".join(md_parts),
                    "rows": rows_out,
                    "verification": summary(fl),
                }
            )
        unverified = [
            {"citation_id": fid_to_cid[f.fid], "statement": f.statement, "scope": f.scope, "label": f.label, "column": f.key, "raw": f.raw, "pdf_page": f.page, "printed_page": infos[f.page].get("printed_page"), "bbox": [round(x, 2) for x in f.cell.bbox], "crop_url": citations[fid_to_cid[f.fid]]["crop_url"]}
            for f in figs
            if not f.verified
        ]
        xchecks = []
        for f in figs:
            for ch in f.checks:
                if ch["rule"] in ("net_assets_equals_equity", "cash_agrees_cash_flow", "profit_agrees_equity_statement", "tci_agrees_equity_statement", "profit_agrees_cash_flow", "agrees_equity_statement"):
                    xchecks.append({"rule": ch["rule"], "citation_id": fid_to_cid[f.fid], "detail": ch["detail"], "related": [fid_to_cid[r] for r in ch["related"] if r in fid_to_cid]})
        return {
            "result_version": RESULT_VERSION,
            "doc_id": sha,
            "statements": statements,
            "verification": {"summary": summary(figs), "cross_checks": xchecks},
            "unverified": unverified,
        }

    # ------------------------------------------------------------------ citations store

    def citations(self) -> dict[str, dict]:
        return self.cache.get_json(self.sha, "citations.json") or {}

    def _store_citations(self, new: dict[str, dict]) -> None:
        with _cit_lock:
            cur = self.cache.get_json(self.sha, "citations.json") or {}
            cur.update(new)
            self.cache.put_json(self.sha, "citations.json", cur)
            self.cache.index_citations(self.sha, list(new))

    def citation(self, cid: str) -> dict | None:
        c = self.citations().get(cid)
        return with_fresh_urls(c) if c else None

    def crop(self, cid: str) -> bytes | None:
        name = f"crops/{cid}.png"
        data = self.cache.get_bytes(self.sha, name)
        if data is not None:
            return data
        c = self.citation(cid)
        if c is None or c.get("type") == "computed":
            return None
        with self._pdf_lock:
            if self._pdf is None:
                self._pdf = pymupdf.open(self.path)
            data = render_crop(self._pdf, c)
        self.cache.put_bytes(self.sha, name, data)
        return data

    def crops(self, cids: list[str]) -> dict[str, bytes]:
        with ThreadPoolExecutor(4) as ex:
            return {cid: d for cid, d in zip(cids, ex.map(self.crop, cids)) if d}

    # ------------------------------------------------------------------ text

    def page_lines(self, page: int, dpi: int = ocr.FULL_DPI) -> list[dict]:
        toks, _ = self.tokens([page], dpi)
        return lines_from_page_tokens(toks[page])

    def ensure_full_notes_index(self) -> list[dict]:
        pm, _, _ = self.page_map()
        if pm.get("notes_index_complete"):
            return pm["notes_index"]
        with _lock_for(self.sha):
            pm = self.cache.get_json(self.sha, "pagemap.json")
            if pm.get("notes_index_complete"):
                return pm["notes_index"]
            notes_pages = [p["page"] for p in pm["pages"] if p["kind"] == "notes"]
            toks, _ = self.tokens(notes_pages, ocr.FULL_DPI)
            cands = []
            for p in notes_pages:
                w, _h = self.page_size(p)
                cands += note_heading_candidates(p, toks[p], w)
            idx = build_notes_index(cands)
            infos = [PageInfo(**p) for p in pm["pages"]]
            _close_note_ranges(idx, infos)
            pm["notes_index"] = idx
            pm["notes_index_complete"] = True
            self.cache.put_json(self.sha, "pagemap.json", pm)
            return idx

    def quote(self, quote: str, pages: list[int] | None = None, section: str | None = None) -> dict | None:
        pm, _, _ = self.page_map()
        pages = pages or [p["page"] for p in pm["pages"]]
        available = {}
        for p in pages:
            cached = self._cached_tokens(p)
            if cached is not None:
                available[p] = lines_from_page_tokens(cached)
        hit = find_quote(available, quote)
        if not hit:
            return None
        page, lines = hit
        info = pm["pages"][page - 1]
        c = build_quote_citation(dict(self.meta, sha256=self.sha), info, page, lines, None, section)
        c["crop_url"] = crop_url(self.sha, c["citation_id"])
        c["viewer_url"] = viewer_url(self.sha, c["citation_id"])
        self._store_citations({c["citation_id"]: c})
        return c

    def _cached_tokens(self, page: int) -> list[dict] | None:
        if self.is_text_page(page):
            return self.cache.get_json(self.sha, f"pages/{page}.tokens.text.json")
        for dpi in (ocr.FULL_DPI, ocr.REREAD_DPI):
            c = self.cache.get_json(self.sha, f"pages/{page}.tokens.{dpi}.json")
            if c is not None:
                return c["tokens"]
        return None

    def paragraphs(self, pages: list[int], section: str | None = None, y_from: dict[int, float] | None = None, y_to: dict[int, float] | None = None) -> list[dict]:
        """Verbatim paragraphs with one quote citation each (one span per line)."""
        pm, _, _ = self.page_map()
        toks, _ = self.tokens(pages, ocr.FULL_DPI)
        out = []
        new_cits = {}
        doc = dict(self.meta, sha256=self.sha)
        for p in pages:
            lines = lines_from_page_tokens(toks[p])
            if y_from and p in y_from:
                lines = [ln for ln in lines if ln["bbox"][1] >= y_from[p] - 1]
            if y_to and p in y_to:
                lines = [ln for ln in lines if ln["bbox"][1] < y_to[p] - 1]
            info = pm["pages"][p - 1]
            for para in group_paragraphs(lines):
                c = build_quote_citation(doc, info, p, para, None, section)
                c["crop_url"] = crop_url(self.sha, c["citation_id"])
                c["viewer_url"] = viewer_url(self.sha, c["citation_id"])
                new_cits[c["citation_id"]] = c
                out.append({"text": c["verbatim"], "pdf_page": p, "printed_page": info.get("printed_page"), "citation_id": c["citation_id"], "provenance": "quoted"})
        self._store_citations(new_cits)
        return out


    def tables(self, page_ranges: dict[int, tuple[float, float]], statement: str, scope: str = "unknown") -> list[dict]:
        """Numeric tables inside y-ranges of pages (notes), cast and cited like the primary statements."""
        from dataclasses import replace

        toks, _ = self.tokens(sorted(page_ranges), ocr.FULL_DPI)
        pm, _, _ = self.page_map()
        infos = {p["page"]: p for p in pm["pages"]}
        out = []
        doc = dict(self.meta, sha256=self.sha)
        new_cits: dict[str, dict] = {}
        for p, (y0, y1) in sorted(page_ranges.items()):
            L = self.layout(p, toks[p], ocr.FULL_DPI)
            secs = []
            for s in L.sections:
                rows = [r for r in s.rows if y0 - 1 <= r.y0 < y1 - 1 and (r.values or len(r.label) <= 60)]
                if any(r.values for r in rows):
                    secs.append(replace(s, rows=rows))
            if not secs:
                continue
            sub = replace(L, sections=secs)
            sc = scope if scope != "unknown" else infos[p].get("scope", "unknown")
            figs = figures_from_layout(sub, statement, sc, f"t{p}_")
            cast_all(figs)
            cast_rows(figs, {(p, i): s for i, s in enumerate(secs)})
            fid_to_cid = {f.fid: citation_id(self.sha, f.page, statement, f.key, round(f.row.y0, 1), round(f.cell.bbox[2], 1), f.raw) for f in figs}
            rows_out = []
            for f in figs:
                c = build_figure_citation(f, doc, infos[p], fid_to_cid)
                c["crop_url"] = crop_url(self.sha, c["citation_id"])
                c["viewer_url"] = viewer_url(self.sha, c["citation_id"])
                new_cits[c["citation_id"]] = c
                rows_out.append({"label": f.label, "column": f.key, "value": float(f.value), "raw": f.raw, "status": f.status, "provenance": c["status"], "citation_id": c["citation_id"], "pdf_page": p})
            out.append(
                {
                    "pdf_page": p,
                    "printed_page": infos[p].get("printed_page"),
                    "markdown": to_markdown(sub, {id(f.cell): f.status for f in figs}),
                    "figures": rows_out,
                    "verification": summary(figs),
                }
            )
        self._store_citations(new_cits)
        return out


_cit_lock = threading.Lock()


def _close_note_ranges(idx: list[dict], infos: list[PageInfo]) -> None:
    if not idx:
        return
    last_notes = max((i.page for i in infos if i.kind == "notes"), default=idx[-1]["start_page"])
    idx[-1]["end_page"] = max(idx[-1]["end_page"], last_notes)


def lines_from_page_tokens(tokens: list[dict]) -> list[dict]:
    lines = []
    for row in build_rows(tokens):
        text = " ".join(t["t"] for t in row)
        bbox = [min(t["b"][0] for t in row), min(t["b"][1] for t in row), max(t["b"][2] for t in row), max(t["b"][3] for t in row)]
        lines.append({"text": text, "bbox": [round(x, 2) for x in bbox], "tokens": row})
    lines.sort(key=lambda ln: ln["bbox"][1])
    return lines


def group_paragraphs(lines: list[dict]) -> list[list[dict]]:
    paras: list[list[dict]] = []
    for ln in lines:
        h = ln["bbox"][3] - ln["bbox"][1]
        if paras:
            prev = paras[-1][-1]
            gap = ln["bbox"][1] - prev["bbox"][3]
            if gap < max(3.0, h * 0.9) and abs(ln["bbox"][0] - paras[-1][0]["bbox"][0]) < 60:
                paras[-1].append(ln)
                continue
        paras.append([ln])
    return paras


def _merge_reread(a: list[Figure], b: list[Figure], reread_pages: set[int]) -> tuple[list[Figure], set[str]]:
    """Combine the 200 DPI and 300 DPI readings cell by cell.

    A figure unverified at 200 DPI is replaced by its 300 DPI reading when that reading verifies.
    A rounding-only match is accepted only when both readings give the same digits.
    """
    by_pos: dict[tuple, list[Figure]] = {}
    for f in b:
        if f.page in reread_pages:
            by_pos.setdefault((f.page, f.key), []).append(f)
    confirmed: set[str] = set()
    remap: dict[str, str] = {}
    pairs: list[tuple[Figure, Figure]] = []
    for f in a:
        if f.page not in reread_pages:
            continue
        cands = [g for g in by_pos.get((f.page, f.key), []) if abs(g.row.yc - f.row.yc) < 3.5 and abs(g.cell.x1 - f.cell.x1) < 8]
        g = min(cands, key=lambda g: abs(g.row.yc - f.row.yc)) if cands else None
        if g is not None:
            remap[g.fid] = f.fid
            pairs.append((f, g))
    for f, g in pairs:
        if f.verified:
            if g.value != f.value:
                f.add_check("reread_disagrees", f"300 DPI re-read gave {g.raw}; kept {f.raw}, which ties arithmetically", [], passed=False)
            continue
        if g.verified:
            prior_raw = f.raw
            # adopt the 300 DPI reading into the 200 DPI layout cell, so tables and citations stay consistent
            f.cell.text, f.cell.num, f.cell.tokens, f.cell.bbox, f.cell.conf = g.cell.text, g.cell.num, g.cell.tokens, g.cell.bbox, g.cell.conf
            f.cell.source, f.cell.ocr_text = g.cell.source, g.cell.ocr_text
            f.value, f.decimals, f.raw, f.low_confidence = g.value, g.decimals, g.raw, g.low_confidence
            f.status = "verified_after_reread"
            f.checks = [dict(ch, related=[remap.get(r, r) for r in ch["related"]]) for ch in g.checks]
            if prior_raw != g.raw:
                f.add_check("reread_corrected", f"200 DPI read {prior_raw}; 300 DPI read {g.raw}, which ties", [], passed=True)
            continue
        if g.value == f.value and (f.provisional_rounding or g.provisional_rounding):
            confirmed.add(f.fid)
        if g.value != f.value:
            f.add_check("reread_disagrees", f"200 DPI read {f.raw}, 300 DPI read {g.raw}", [], passed=False)
    return a, confirmed


def resolve_statements(statements: list[str] | None) -> list[str]:
    if not statements:
        return list(PRIMARY)
    out = []
    for s in statements:
        k = STATEMENT_ALIASES.get(s.lower().strip().replace(" ", "_"))
        if k is None:
            raise ValueError(f"unknown statement {s!r}; use income, balance, cash_flow or equity")
        out.append(k)
    return out


def provenance_legend() -> dict:
    return PROVENANCE
