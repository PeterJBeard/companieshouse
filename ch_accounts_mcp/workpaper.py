"""Tick-and-reference workpaper export as Excel and PDF (brief section 15.9).

This is an evidence pack in the shape firms already use. It does not claim to meet any auditing standard.
"""

from __future__ import annotations

import io
import time
from datetime import datetime, timezone
from pathlib import Path

import pymupdf

from .citations import PROVENANCE, check_answer
from .pipeline import TOOL_VERSION, Document
from .server_auth import sign


def _header(doc: Document) -> list[tuple[str, str]]:
    m = doc.meta
    return [
        ("Company", m.get("company_name") or m.get("company_name_on_document") or ""),
        ("Company number", m.get("company_number") or ""),
        ("Filing (transaction id)", m.get("transaction_id") or ""),
        ("Filing date", m.get("filing_date") or ""),
        ("Made-up date", m.get("made_up_date") or ""),
        ("Companies House URL", m.get("source_url") or ""),
        ("Document SHA-256", doc.sha),
        ("Retrieved at", m.get("retrieved_at") or ""),
        ("Tool version", TOOL_VERSION),
        ("OCR model", m.get("ocr_model") or ("text layer" if m.get("text_layer") else "")),
        ("Prepared at", datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")),
    ]


def _checks_text(c: dict) -> str:
    return "; ".join(f"{ch['rule']}: {ch['detail']}" for ch in c.get("checks", []) if ch.get("passed", True))


def export_workpaper(doc: Document, citation_ids: list[str] | None = None, answer: str | None = None, reviewer: str | None = None) -> dict:
    t0 = time.perf_counter()
    cits = doc.citations()
    unmatched: list[dict] = []
    judgement: list[str] = []
    ids = list(citation_ids or [])
    if answer:
        chk = check_answer(doc, answer)
        ids += [i["citation_id"] for i in chk["items"] if i["status"] == "matched" and i["citation_id"] not in ids]
        unmatched = [i for i in chk["items"] if i["status"] == "unmatched"]
    missing = [i for i in ids if i not in cits]
    items = [cits[i] for i in ids if i in cits]
    figures = [c for c in items if c.get("type") == "figure" and c["status"] == "extracted_verified"]
    computed = [c for c in items if c.get("type") == "computed"]
    quotes = [c for c in items if c.get("type") == "quote"]
    unverified = [c for c in items if c.get("type") == "figure" and c["status"] != "extracted_verified"]
    crops = doc.crops([c["citation_id"] for c in figures + unverified + quotes])

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    outdir = doc.cache.doc_dir(doc.sha) / "workpapers"
    outdir.mkdir(parents=True, exist_ok=True)
    xlsx = outdir / f"workpaper_{stamp}.xlsx"
    pdf = outdir / f"workpaper_{stamp}.pdf"
    _write_xlsx(xlsx, doc, figures, computed, quotes, unverified, unmatched, judgement, crops, reviewer)
    _write_pdf(pdf, doc, figures, computed, quotes, unverified, unmatched, crops, reviewer)
    from .config import settings

    sig = sign(doc.sha)
    base = settings().public_base_url
    q = f"?s={sig}" if sig else ""
    return {
        "doc_id": doc.sha,
        "xlsx_path": str(xlsx),
        "pdf_path": str(pdf),
        "xlsx_url": f"{base}/workpaper/{doc.sha}/{xlsx.name}{q}",
        "pdf_url": f"{base}/workpaper/{doc.sha}/{pdf.name}{q}",
        "counts": {"verified_figures": len(figures), "computed": len(computed), "quotes": len(quotes), "unverified": len(unverified), "unmatched_claims": len(unmatched)},
        "missing_citation_ids": missing,
        "note": "Evidence pack for review. It does not claim compliance with any auditing standard; the firm decides that.",
        "timings_ms": {"export": round((time.perf_counter() - t0) * 1000, 1)},
        "cache_hit": False,
    }


def _row_values(c: dict) -> list:
    return [
        c["citation_id"],
        c.get("statement") or c.get("metric") or c.get("section") or "",
        c.get("label") or (c.get("verbatim") or "")[:200] or c.get("formula", ""),
        c.get("raw") or c.get("display") or "",
        c.get("value"),
        c.get("unit") or "",
        c.get("period") or (str(c.get("year")) if c.get("year") else ""),
        c.get("scope") or "",
        c.get("pdf_page") or "",
        c.get("printed_page") or "",
        PROVENANCE.get(c["status"], {}).get("label", c["status"]),
        _checks_text(c) if c.get("type") != "computed" else c.get("formula", ""),
    ]


_COLS = ["Citation", "Statement / metric", "Line item / text", "As filed", "Value", "Unit", "Period", "Scope", "PDF page", "Printed page", "Provenance", "Checks / formula", "Crop", "Reviewer tick", "Reviewer", "Reviewed at"]


def _write_xlsx(path: Path, doc: Document, figures, computed, quotes, unverified, unmatched, judgement, crops, reviewer) -> None:
    from openpyxl import Workbook
    from openpyxl.drawing.image import Image as XLImage
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "Workpaper"
    bold = Font(bold=True)
    r = 1
    for k, v in _header(doc):
        ws.cell(r, 1, k).font = bold
        ws.cell(r, 2, v)
        r += 1
    r += 1

    def table(title: str, rows: list[dict], fill: str) -> None:
        nonlocal r
        ws.cell(r, 1, title).font = Font(bold=True, size=12)
        r += 1
        for j, h in enumerate(_COLS, 1):
            c = ws.cell(r, j, h)
            c.font = bold
            c.fill = PatternFill("solid", fgColor=fill)
        r += 1
        for c in rows:
            for j, v in enumerate(_row_values(c), 1):
                ws.cell(r, j, v).alignment = Alignment(wrap_text=True, vertical="top")
            ws.cell(r, 14, "")
            ws.cell(r, 15, reviewer or "")
            png = crops.get(c["citation_id"])
            if png:
                img = XLImage(io.BytesIO(png))
                scale = min(1.0, 420 / img.width)
                img.width, img.height = int(img.width * scale), int(img.height * scale)
                ws.add_image(img, f"M{r}")
                ws.row_dimensions[r].height = max(30, img.height * 0.75 + 4)
            r += 1
        r += 1

    table("Verified figures", figures, "D6F5DD")
    table("Computed figures", computed, "D6E6F7")
    table("Quotes", quotes, "E9E0F7")
    table("Unverified figures - check against the source", unverified, "FCE8C8")
    ws.cell(r, 1, "Unmatched claims from the answer (no source found)").font = Font(bold=True, size=12)
    r += 1
    for u in unmatched:
        ws.cell(r, 1, u["text"])
        ws.cell(r, 2, u.get("reason", ""))
        r += 1
    r += 1
    ws.cell(r, 1, "Reviewer sign-off").font = Font(bold=True, size=12)
    for label in ("Prepared by", "Date", "Reviewed by", "Date"):
        r += 1
        ws.cell(r, 1, label).font = bold
        ws.cell(r, 2, "")
    widths = [16, 18, 40, 14, 14, 9, 9, 9, 8, 8, 18, 50, 60, 10, 14, 14]
    for j, w in enumerate(widths, 1):
        ws.column_dimensions[chr(64 + j)].width = w
    wb.save(path)


def _write_pdf(path: Path, doc: Document, figures, computed, quotes, unverified, unmatched, crops, reviewer) -> None:
    out = pymupdf.open()
    W, H = 595, 842
    page = out.new_page(width=W, height=H)
    y = 40

    def text(s: str, size: float = 9, bold: bool = False, color=(0, 0, 0)) -> None:
        nonlocal page, y
        lines = max(1, int(len(s) / (95 * 9 / size)) + 1 + s.count("\n"))
        hgt = lines * size * 1.35 + 2
        if y + hgt > H - 40:
            page = out.new_page(width=W, height=H)
            y = 40
        page.insert_textbox(pymupdf.Rect(40, y, W - 40, y + hgt + 4), s, fontsize=size, fontname="hebo" if bold else "helv", color=color)
        y += hgt

    def image(png: bytes) -> None:
        nonlocal page, y
        pix = pymupdf.Pixmap(png)
        w = min(W - 80, pix.width * 72 / 200)
        h = pix.height * w / pix.width
        if y + h > H - 40:
            page = out.new_page(width=W, height=H)
            y = 40
        page.insert_image(pymupdf.Rect(40, y, 40 + w, y + h), stream=png)
        y += h + 6

    text("Workpaper: figures traced to the Companies House filing", 13, True)
    for k, v in _header(doc):
        text(f"{k}: {v}", 8)
    y += 6
    sections = [("Verified figures", figures, (0.1, 0.5, 0.2)), ("Computed figures", computed, (0.04, 0.36, 0.68)), ("Quotes", quotes, (0.44, 0.26, 0.76)), ("Unverified figures - check against the source", unverified, (0.7, 0.42, 0))]
    for title, rows, colour in sections:
        if not rows:
            continue
        text(title, 11, True, colour)
        for c in rows:
            v = _row_values(c)
            prov = PROVENANCE.get(c["status"], {})
            text(f"[{prov.get('label', c['status'])}] {v[2]}   {v[3]} {v[5]}   {v[7]} {v[6]}   PDF page {v[8]}, printed page {v[9] or '-'}   [{v[0]}]", 8.5, True)
            if v[11]:
                text(str(v[11])[:600], 7.5)
            png = crops.get(c["citation_id"])
            if png:
                image(png)
            text(f"Reviewer tick: [  ]   Reviewer: {reviewer or '________'}   Date: ________", 7.5)
            y += 4
    if unmatched:
        text("Unmatched claims from the answer (no source found)", 11, True, (0.6, 0, 0))
        for u in unmatched:
            text(f"UNMATCHED: {u['text']}: {u.get('reason', '')}", 8)
    y += 10
    text("Reviewer sign-off", 11, True)
    text("Prepared by: ____________________   Date: __________\n\nReviewed by: ____________________   Date: __________", 9)
    text("This evidence pack does not claim compliance with any auditing standard; the firm decides that.", 7)
    out.save(str(path))
    out.close()
