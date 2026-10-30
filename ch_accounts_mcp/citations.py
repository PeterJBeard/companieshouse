"""Citation objects, inline crops, verbatim quote matching and answer checking (brief section 15)."""

from __future__ import annotations

import hashlib
import io
import re
from decimal import Decimal
from typing import Any, Iterable

import pymupdf

from .layout import Cell, unit_multiplier
from .verify import VERIFIED, Figure

PROVENANCE = {
    "extracted_verified": {"label": "Verified", "icon": "✔", "colour": "#1a7f37"},
    "extracted_unverified": {"label": "Unverified - check the source", "icon": "⚠", "colour": "#b26a00"},
    "computed": {"label": "Computed", "icon": "∑", "colour": "#0b5cad"},
    "quoted": {"label": "Quoted verbatim", "icon": "❝", "colour": "#6f42c1"},
    "judgement": {"label": "Analysis, not from the filing", "icon": "◌", "colour": "#6e7781"},
}

CROP_DPI = 200


def citation_id(*parts: Any) -> str:
    h = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()
    return "c_" + h[:12]


def _bbox(cells: Iterable[Cell]) -> list[float]:
    cs = list(cells)
    return [round(min(c.bbox[0] for c in cs), 2), round(min(c.bbox[1] for c in cs), 2), round(max(c.bbox[2] for c in cs), 2), round(max(c.bbox[3] for c in cs), 2)]


def _line_spans(role: str, cells: list[Cell]) -> list[dict]:
    """One span per visual line, so a wrapped label highlights line by line."""
    lines: list[list[Cell]] = []
    for c in sorted(cells, key=lambda c: (c.bbox[1], c.bbox[0])):
        if lines and abs(lines[-1][0].bbox[1] - c.bbox[1]) < 4:
            lines[-1].append(c)
        else:
            lines.append([c])
    return [{"role": role, "text": " ".join(c.text for c in sorted(ln, key=lambda c: c.bbox[0])), "bbox": _bbox(ln)} for ln in lines]


def figure_status(f: Figure) -> str:
    return "extracted_verified" if f.status in VERIFIED else "extracted_unverified"


def build_figure_citation(f: Figure, doc: dict, page_info: dict, fid_to_cid: dict[str, str]) -> dict:
    seen = set()
    ordered = []
    for c in sorted(f.row.label_cells, key=lambda c: (c.bbox[1], c.bbox[0])) + sorted([c for c in f.row.cells if c.role in ("note", "value")], key=lambda c: c.bbox[0]):
        if id(c) not in seen:
            seen.add(id(c))
            ordered.append(c)
    verbatim = " ".join(c.text for c in ordered)
    spans: list[dict] = []
    if f.row.label_cells:
        spans += _line_spans("label", f.row.label_cells)
    if f.row.note_cell is not None:
        spans.append({"role": "note", "text": f.row.note_cell.text, "bbox": _bbox([f.row.note_cell])})
    spans.append({"role": "value", "text": f.cell.text, "bbox": _bbox([f.cell])})
    unit_cells = [h for h in f.column.header_cells if _is_unit_text(h.text)]
    other_hdr = [h for h in f.column.header_cells if not _is_unit_text(h.text)]
    for h in sorted(other_hdr, key=lambda c: (c.bbox[1], c.bbox[0])):
        spans.append({"role": "header", "text": h.text, "bbox": _bbox([h])})
    for u in unit_cells:
        spans.append({"role": "unit", "text": u.text, "bbox": _bbox([u])})
    header_text = " ".join(h.text for h in sorted(f.column.header_cells, key=lambda c: (c.bbox[1], c.bbox[0])))
    cid = fid_to_cid[f.fid]
    checks = []
    for ch in f.checks:
        checks.append({"rule": ch["rule"], "detail": ch["detail"], "passed": ch.get("passed", True), "citation_ids": [fid_to_cid[r] for r in ch.get("related", []) if r in fid_to_cid]})
    return {
        "citation_id": cid,
        "type": "figure",
        "doc_sha256": doc["sha256"],
        "company_number": doc.get("company_number"),
        "company_name": doc.get("company_name"),
        "transaction_id": doc.get("transaction_id"),
        "filing_date": doc.get("filing_date"),
        "made_up_date": doc.get("made_up_date"),
        "source_url": doc.get("source_url"),
        "retrieved_at": doc.get("retrieved_at"),
        "pdf_page": f.page,
        "printed_page": page_info.get("printed_page"),
        "statement": f.statement,
        "scope": f.scope,
        "label": f.label,
        "note": f.note,
        "column": f.key,
        "column_header": header_text,
        "value": float(f.value),
        "value_exact": str(f.value),
        "raw": f.raw,
        "verbatim": verbatim,
        "row_bbox": _bbox(ordered) if ordered else _bbox([f.cell]),
        "spans": spans,
        "unit": f.unit,
        "period": f.period,
        "year": f.year,
        "restated": f.restated,
        "ocr_confidence": round(min((t["c"] for t in f.cell.tokens), default=1.0), 4),
        "ocr_low_confidence_substitution": f.low_confidence,
        "status": figure_status(f),
        "verification_status": f.status,
        "checks": checks,
        "crop_url": None,
        "viewer_url": None,
    }


def _is_unit_text(t: str) -> bool:
    from .layout import is_unit

    return is_unit(t)


def has_required_spans(c: dict) -> bool:
    """Trust test 15.12(2): a figure citation shows label, value, column header and unit."""
    roles = {s["role"] for s in c["spans"]}
    return {"label", "value", "header"}.issubset(roles) and ("unit" in roles or bool(c.get("unit")))


def tokens_in_bbox(tokens: list[dict], bbox: list[float], slack: float = 0.6) -> list[dict]:
    x0, y0, x1, y1 = bbox
    out = []
    for t in tokens:
        b = t["b"]
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        if x0 - slack <= cx <= x1 + slack and y0 - slack <= cy <= y1 + slack:
            out.append(t)
    return out


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def span_matches_tokens(span: dict, tokens: list[dict]) -> bool:
    """Trust test 15.12(1): stored text equals the OCR tokens inside the cited box."""
    inside = sorted(tokens_in_bbox(tokens, span["bbox"]), key=lambda t: (round(t["b"][1] / 4), t["b"][0]))
    return _norm_ws(" ".join(t["t"] for t in inside)).replace(" ", "") == _norm_ws(span["text"]).replace(" ", "")


# --------------------------------------------------------------------------- quotes


def _norm_quote(s: str) -> str:
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"').replace("—", "-").replace("–", "-")
    return re.sub(r"\s+", " ", s).strip().lower()


def find_quote(page_lines: dict[int, list[dict]], quote: str) -> tuple[int, list[dict]] | None:
    """Locate a verbatim passage across consecutive lines. page_lines[p] = [{"text", "bbox", "tokens"}...]."""
    q = _norm_quote(quote).strip(" .\"'")
    if len(q) < 8:
        return None
    for page, lines in page_lines.items():
        texts = [_norm_quote(ln["text"]) for ln in lines]
        joined = ""
        starts = []
        for t in texts:
            starts.append(len(joined))
            joined += t + " "
        i = joined.find(q)
        if i < 0:
            # tolerate a hyphenated line break: "manage- ment" -> "management"
            alt = re.sub(r"- ", "", joined)
            if q not in alt:
                continue
            i = None
        if i is None:
            # fall back to line-level containment by words
            words = q.split()
            hits = [k for k, t in enumerate(texts) if words[0] in t]
            if not hits:
                continue
            k0 = hits[0]
            k1 = min(len(lines) - 1, k0 + max(0, len(q) // 60 + 1))
            return page, lines[k0 : k1 + 1]
        j = i + len(q)
        sel = [k for k, s in enumerate(starts) if s < j and s + len(texts[k]) > i]
        return page, [lines[k] for k in sel]
    return None


def build_quote_citation(doc: dict, page_info: dict, page: int, lines: list[dict], quote: str | None = None, section: str | None = None) -> dict:
    text = " ".join(ln["text"] for ln in lines)
    cid = citation_id(doc["sha256"], page, "quote", *(tuple(ln["bbox"]) for ln in lines))
    return {
        "citation_id": cid,
        "type": "quote",
        "doc_sha256": doc["sha256"],
        "company_number": doc.get("company_number"),
        "company_name": doc.get("company_name"),
        "transaction_id": doc.get("transaction_id"),
        "filing_date": doc.get("filing_date"),
        "made_up_date": doc.get("made_up_date"),
        "source_url": doc.get("source_url"),
        "retrieved_at": doc.get("retrieved_at"),
        "pdf_page": page,
        "printed_page": page_info.get("printed_page"),
        "section": section,
        "verbatim": quote if quote is not None else text,
        "lines_verbatim": text,
        "spans": [{"role": "quote", "text": ln["text"], "bbox": ln["bbox"]} for ln in lines],
        "status": "quoted",
        "ocr_confidence": round(min((t["c"] for ln in lines for t in ln.get("tokens", [])), default=1.0), 4),
        "checks": [],
        "crop_url": None,
        "viewer_url": None,
    }


# --------------------------------------------------------------------------- crops


def render_crop(pdf: pymupdf.Document, cit: dict, dpi: int = CROP_DPI) -> bytes:
    """PNG of the cited row plus the column header strip, with the value highlighted."""
    from PIL import Image, ImageDraw

    page = pdf[cit["pdf_page"] - 1]
    pr = page.rect
    spans = cit["spans"]
    if cit.get("type") == "figure":
        row = cit["row_bbox"]
        hdr = [s for s in spans if s["role"] in ("header", "unit")]
        x0 = max(pr.x0, min([row[0]] + [s["bbox"][0] for s in spans]) - 6)
        x1 = min(pr.x1, max([row[2]] + [s["bbox"][2] for s in spans]) + 6)
        strips = []
        if hdr:
            strips.append(pymupdf.Rect(x0, min(s["bbox"][1] for s in hdr) - 3, x1, max(s["bbox"][3] for s in hdr) + 3))
        strips.append(pymupdf.Rect(x0, row[1] - 4, x1, row[3] + 4))
    else:
        x0 = max(pr.x0, min(s["bbox"][0] for s in spans) - 6)
        x1 = min(pr.x1, max(s["bbox"][2] for s in spans) + 6)
        strips = [pymupdf.Rect(x0, min(s["bbox"][1] for s in spans) - 4, x1, max(s["bbox"][3] for s in spans) + 4)]
    scale = dpi / 72.0
    images = []
    for r in strips:
        r = r & pr
        pix = page.get_pixmap(dpi=dpi, clip=r, alpha=False)
        img = Image.frombytes("RGB", (pix.w, pix.h), pix.samples)
        ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(ov)
        for s in spans:
            b = s["bbox"]
            if b[3] < r.y0 or b[1] > r.y1:
                continue
            box = [(b[0] - r.x0) * scale - 2, (b[1] - r.y0) * scale - 2, (b[2] - r.x0) * scale + 2, (b[3] - r.y0) * scale + 2]
            if s["role"] in ("value", "quote"):
                d.rectangle(box, fill=(255, 214, 0, 90), outline=(214, 140, 0, 255), width=2)
            else:
                d.rectangle(box, fill=(255, 214, 0, 40))
        images.append(Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB"))
    gap = 6
    W = max(i.width for i in images)
    H = sum(i.height for i in images) + gap * (len(images) - 1)
    out = Image.new("RGB", (W, H), (235, 235, 235))
    y = 0
    for i in images:
        out.paste(i, (0, y))
        y += i.height + gap
    buf = io.BytesIO()
    out.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# --------------------------------------------------------------------------- answer checking

_NUM_IN_TEXT = re.compile(
    r"(?P<neg>\(|-|−)?(?P<cur>[£$€])?\s?(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\)?"
    r"(?:\s?(?P<unit>bn|billion|m|mn|million|k|thousand|'000|000s|%|per cent|percent)\b|(?P<pct>%))?",
    re.I,
)
_QUOTE = re.compile(r"[\"“]([^\"”]{12,})[\"”]")
_CITE_TAG = re.compile(r"\[(c_[0-9a-f]{12})\]")


def _multiplier(unit: str | None) -> Decimal:
    if not unit:
        return Decimal(1)
    u = unit.lower()
    if u in ("bn", "billion"):
        return Decimal(10) ** 9
    if u in ("m", "mn", "million"):
        return Decimal(10) ** 6
    if u in ("k", "thousand", "'000", "000s"):
        return Decimal(1000)
    return Decimal(1)


def extract_claims(text: str) -> list[dict]:
    claims = []
    quote_spans = []
    for m in _QUOTE.finditer(text):
        claims.append({"kind": "quote", "text": m.group(1), "start": m.start(), "end": m.end()})
        quote_spans.append((m.start(), m.end()))
    for m in _NUM_IN_TEXT.finditer(text):
        if any(a <= m.start() < b for a, b in quote_spans):
            continue
        num = m.group("num")
        unit = m.group("unit") or m.group("pct")
        pre = text[max(0, m.start() - 12) : m.start()].lower()
        post = text[m.end() : m.end() + 3]
        if _CITE_TAG.match(text[m.start() - 1 : m.end() + 15] if m.start() else ""):
            continue
        if text[max(0, m.start() - 2) : m.start()] == "c_" or re.search(r"c_[0-9a-f]*$", text[max(0, m.start() - 14) : m.start()]):
            continue
        is_pct = unit is not None and unit.lower() in ("%", "per cent", "percent")
        if not m.group("cur") and not unit:
            plain = num.replace(",", "")
            if re.fullmatch(r"(19|20)\d{2}", plain):
                continue  # a year
            if re.search(r"(page|pages|note|notes|section|s\.|no\.|number|p\.)\s*$", pre):
                continue
            if re.fullmatch(r"\d{1,2}", plain) and re.search(r"(january|february|march|april|may|june|july|august|september|october|november|december)", post.lower() + text[m.end() : m.end() + 12].lower()):
                continue  # a day of month
            if re.search(r"(january|february|march|april|may|june|july|august|september|october|november|december)\s*$", pre):
                continue
        try:
            val = Decimal(num.replace(",", ""))
        except Exception:
            continue
        dec = len(num.split(".")[1]) if "." in num else 0
        tag = None
        tm = _CITE_TAG.search(text[m.end() : m.end() + 20])
        if tm and text[m.end() : m.end() + tm.start()].strip() in ("", ",", ")"):
            tag = tm.group(1)
        claims.append(
            {
                "kind": "percentage" if is_pct else "number",
                "text": m.group(0).strip(),
                "start": m.start(),
                "end": m.end(),
                "value": val,
                "decimals": dec,
                "multiplier": Decimal(1) if is_pct else _multiplier(unit),
                "negative": bool(m.group("neg")) and m.group(0).strip().startswith(("(", "-", "−")),
                "cited": tag,
            }
        )
    claims.sort(key=lambda c: c["start"])
    return claims


def match_number(claim: dict, figures: list[dict]) -> tuple[dict | None, str]:
    """Match a number in prose to a figure citation, allowing unit conversion and rounding."""
    stated = claim["value"] * claim["multiplier"]
    half = Decimal(5).scaleb(-(claim["decimals"] + 1)) * claim["multiplier"]
    best = None
    for f in figures:
        if f.get("type") == "computed":
            fv = Decimal(str(f["value_exact"]))
            if claim["kind"] == "percentage" and f.get("is_ratio"):
                fv = fv * 100
            elif claim["kind"] == "percentage":
                continue
            mult = Decimal(1)
        else:
            if claim["kind"] == "percentage":
                continue
            fv = Decimal(f["value_exact"])
            mult = unit_multiplier(f.get("unit"))
        actual = abs(fv * mult)
        if abs(actual - abs(stated)) <= half:
            exact = actual == abs(stated)
            score = (0 if exact else 1, 0 if f.get("status") == "extracted_verified" else 1)
            if best is None or score < best[0]:
                best = (score, f, "exact" if exact else "rounded")
    if best is None:
        return None, "unmatched"
    return best[1], best[2]
