"""Rebuild rows, columns and headers from positioned tokens (brief section 7)."""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

# --------------------------------------------------------------------------- number parsing

NUM_RE1 = re.compile(r"^\(?-?[£$€]?\s?\d{1,3}(,\d{3})*(\.\d+)?\)?$")
NUM_RE2 = re.compile(r"^\(?-?\d+(\.\d+)?\)?$")
DASHES = {"-", "–", "—", "−", "--"}
_CONFUSABLE = str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1", "S": "5", "B": "8"})
_CURRENCY_LEAD = re.compile(r"^(\(?)(?:[£$€]|f|E)(?=\d)")
YEAR_RE = re.compile(r"^(19|20)\d{2}$")
UNIT_RE = re.compile(r"^(£|\$|€|f|E)\s*('?0{3}|m|bn|million|k)?$|^(£|f|E)'?000$|^\$?m$|^US\$('?000|m)?$|^€('?000|m)$", re.I)


@dataclass
class ParsedNumber:
    value: Decimal
    decimals: int
    raw: str
    low_confidence: bool = False
    is_dash: bool = False


def normalise_numeric(raw: str) -> tuple[str, bool] | None:
    """Return (canonical numeric string, substituted) or None if the token is not numeric."""
    s = raw.strip().replace(" ", "")
    if not s:
        return None
    if s in DASHES or re.fullmatch(r"\(?[-–—−]\)?", s):
        return "-", False
    s = s.replace("−", "-")
    sub = False
    m = _CURRENCY_LEAD.match(s)
    if m:
        s = m.group(1) + s[m.end():]
    if NUM_RE1.match(s) or NUM_RE2.match(s):
        return s, False
    # OCR confusables, only inside an otherwise numeric token
    digits = sum(ch.isdigit() for ch in s)
    if digits and len(s) >= 2:
        t = s.translate(_CONFUSABLE)
        if t != s and sum(1 for a, b in zip(s, t) if a != b) <= 2 and digits >= len(re.sub(r"[(),.\-]", "", s)) / 2:
            if NUM_RE1.match(t) or NUM_RE2.match(t):
                return t, True
    # thousands separator read as a full stop: 1.168.3 -> 1,168.3
    if re.fullmatch(r"\(?\d{1,3}(\.\d{3})+\.\d+\)?", s):
        parts = s.split(".")
        return ",".join(parts[:-1]) + "." + parts[-1], True
    return None


def parse_number(raw: str) -> ParsedNumber | None:
    n = normalise_numeric(raw)
    if n is None:
        return None
    s, sub = n
    if s == "-":
        return ParsedNumber(Decimal(0), 0, raw, sub, True)
    neg = (s.startswith("(") and s.endswith(")")) or s.startswith("-") or s.startswith("(-")
    if s.count("(") != s.count(")"):
        # an unbalanced bracket is still read as negative but flagged
        neg = s.startswith("(") or s.endswith(")") or s.startswith("-")
        sub = True
    core = re.sub(r"[^\d.]", "", s)
    try:
        v = Decimal(core)
    except InvalidOperation:
        return None
    dec = len(core.split(".")[1]) if "." in core else 0
    return ParsedNumber(-v if neg else v, dec, raw, sub, False)


def is_year(text: str) -> bool:
    return bool(YEAR_RE.match(text.strip()))


def is_unit(text: str) -> bool:
    return bool(UNIT_RE.match(text.strip().replace("’", "'")))


def canonical_unit(text: str) -> str:
    s = text.strip().replace("’", "'")
    s = re.sub(r"^[fE](?=('?000|m|bn|k)?$)", "£", s)
    s = s.replace("£000", "£'000").replace("$000", "$'000").replace("€000", "€'000")
    return {"£m": "£m", "m": "£m"}.get(s, s)


def unit_multiplier(unit: str | None) -> Decimal:
    if not unit:
        return Decimal(1)
    u = unit.lower()
    if "bn" in u:
        return Decimal(10) ** 9
    if u.endswith("m") or "million" in u:
        return Decimal(10) ** 6
    if "000" in u or u.endswith("k"):
        return Decimal(1000)
    return Decimal(1)


# --------------------------------------------------------------------------- data model


@dataclass
class Cell:
    text: str
    bbox: list[float]
    conf: float
    tokens: list[dict]
    kind: str = "text"  # text | number
    num: ParsedNumber | None = None
    column: int | None = None
    role: str = "label"  # label | note | value | header | other

    @property
    def x0(self) -> float:
        return self.bbox[0]

    @property
    def x1(self) -> float:
        return self.bbox[2]


@dataclass
class Row:
    cells: list[Cell]
    y0: float
    y1: float
    kind: str = "data"  # data | header | text
    label: str = ""
    label_cells: list[Cell] = field(default_factory=list)
    note: str | None = None
    note_cell: Cell | None = None
    values: dict[str, Cell] = field(default_factory=dict)

    @property
    def yc(self) -> float:
        return (self.y0 + self.y1) / 2


@dataclass
class Column:
    index: int
    x0: float
    x1: float
    members: list[Cell] = field(default_factory=list)
    is_note: bool = False
    header_cells: list[Cell] = field(default_factory=list)
    year: int | None = None
    scope: str | None = None
    unit: str | None = None
    header_text: str = ""
    key: str = ""
    period: str | None = None
    restated: bool = False


@dataclass
class Section:
    header_rows: list[Row]
    rows: list[Row]
    columns: list[Column]
    unit: str | None = None

    def value_columns(self) -> list[Column]:
        return [c for c in self.columns if not c.is_note]

    def column_keys(self) -> list[str]:
        seen: list[str] = []
        for c in self.value_columns():
            if c.key not in seen:
                seen.append(c.key)
        return seen


@dataclass
class PageLayout:
    page: int
    width: float
    height: float
    rows: list[Row]
    sections: list[Section]
    unit: str | None
    title_lines: list[str]


# --------------------------------------------------------------------------- rows and cells


def _median(xs: list[float], default: float) -> float:
    return statistics.median(xs) if xs else default


def build_rows(tokens: list[dict]) -> list[list[dict]]:
    """Group tokens into visual rows by vertical centre (brief 7.1)."""
    toks = [t for t in tokens if t["t"].strip()]
    if not toks:
        return []
    heights = [t["b"][3] - t["b"][1] for t in toks]
    mh = _median(heights, 8.0)
    toks.sort(key=lambda t: (t["b"][1] + t["b"][3]) / 2)
    rows: list[list[dict]] = []
    centres: list[float] = []
    for t in toks:
        c = (t["b"][1] + t["b"][3]) / 2
        if rows and abs(c - centres[-1]) <= mh / 2:
            rows[-1].append(t)
            centres[-1] = sum((x["b"][1] + x["b"][3]) / 2 for x in rows[-1]) / len(rows[-1])
        else:
            rows.append([t])
            centres.append(c)
    for r in rows:
        r.sort(key=lambda t: t["b"][0])
    return rows


def _char_width(tokens: list[dict]) -> float:
    ws = [(t["b"][2] - t["b"][0]) / max(1, len(t["t"])) for t in tokens if len(t["t"]) >= 3]
    return _median(ws, 5.0)


def _numeric_fragment(s: str) -> bool:
    return bool(re.fullmatch(r"[(£$€]?[\d,.\-–—()]*\)?", s)) and s != ""


def build_cells(row: list[dict], char_w: float) -> list[Cell]:
    """Merge adjacent tokens into cells (brief 7.2); never merge a number with text."""
    cells: list[Cell] = []
    for t in row:
        is_num = normalise_numeric(t["t"]) is not None
        if cells:
            prev = cells[-1]
            gap = t["b"][0] - prev.x1
            if gap < char_w * 1.1:
                prev_num = prev.kind == "number"
                joined = prev.text + t["t"]
                if prev_num and normalise_numeric(joined) is not None and gap < char_w * 0.8:
                    _extend(prev, t, joined)
                    continue
                if not prev_num and not is_num and not _numeric_fragment(t["t"]):
                    _extend(prev, t, prev.text + " " + t["t"])
                    continue
                if prev.text in ("(", "£", "(£", "$", "€") and is_num and gap < char_w * 0.8:
                    _extend(prev, t, prev.text + t["t"])
                    prev.kind = "number"
                    continue
        cells.append(Cell(t["t"], list(t["b"]), t["c"], [t], "number" if is_num else "text"))
    for c in cells:
        if c.kind == "number":
            c.num = parse_number(c.text)
            if c.num is None:
                c.kind = "text"
    return cells


def _extend(cell: Cell, t: dict, text: str) -> None:
    cell.text = text
    cell.tokens.append(t)
    b = t["b"]
    cell.bbox = [min(cell.bbox[0], b[0]), min(cell.bbox[1], b[1]), max(cell.bbox[2], b[2]), max(cell.bbox[3], b[3])]
    cell.conf = min(cell.conf, t["c"])


def _reclassify_in_text_numbers(cells: list[Cell], char_w: float) -> None:
    """A number sitting right next to words ("At 31 December 2024") is part of the text."""
    for i, c in enumerate(cells):
        if c.kind != "number":
            continue
        left = cells[i - 1] if i > 0 else None
        right = cells[i + 1] if i + 1 < len(cells) else None
        near_left = left is not None and left.kind == "text" and c.x0 - left.x1 < char_w * 1.8 and not is_unit(left.text)
        near_right = right is not None and right.kind == "text" and right.x0 - c.x1 < char_w * 1.8 and not is_unit(right.text)
        if near_left or near_right:
            c.kind, c.num = "text", None


_HEADER_WORDS = re.compile(
    r"^(notes?|group|company|parent|consolidated|total|restated|\*|as|and|unaudited|audited|weeks?|ended|year|period|months?|52|53|"
    r"\(restated\)|\(as restated\)|\(note \d+\)|£|\$|€|£m|£'000|£000|\$m|\$'000|€m|€'000|fm|em|m)$",
    re.I,
)


def _row_is_header(cells: list[Cell], width: float) -> bool:
    nums = [c for c in cells if c.kind == "number"]
    texts = [c for c in cells if c.kind == "text"]
    if nums and all(is_year(c.text) and "," not in c.text for c in nums):
        others = [c for c in texts if not _all_header_words(c.text)]
        if not others or all(c.x0 > width * 0.35 for c in others) or len(nums) >= 2:
            return True
    if not nums and texts:
        if all(_all_header_words(c.text) or is_unit(c.text) for c in texts):
            return True
        if all(c.x0 > width * 0.40 for c in texts):
            return True
    return False


def _all_header_words(text: str) -> bool:
    return all(_HEADER_WORDS.match(w) or is_unit(w) or is_year(w) for w in text.replace("’", "'").split())


# --------------------------------------------------------------------------- columns


def cluster_columns(cells: list[Cell], width: float, gap: float = 15.0) -> list[Column]:
    """Cluster numeric cells by right edge (brief 7.4)."""
    pts = sorted(((c.x1, c) for c in cells if c.x1 > width * 0.3), key=lambda p: p[0])
    groups: list[list[Cell]] = []
    last = None
    for x1, c in pts:
        if last is None or x1 - last > gap:
            groups.append([c])
        else:
            groups[-1].append(c)
        last = x1
    cols = []
    for g in groups:
        if len(g) < 2:
            continue
        cols.append(Column(len(cols), min(c.x0 for c in g), max(c.x1 for c in g), g))
    return cols


def _assign_column(cell: Cell, cols: list[Column], tol: float = 15.0) -> Column | None:
    best, bd = None, None
    for col in cols:
        right = min(c.x1 for c in col.members) - tol
        left_ok = cell.x1 >= right
        if left_ok and cell.x1 <= col.x1 + tol:
            d = abs(cell.x1 - statistics.median([c.x1 for c in col.members]))
            if bd is None or d < bd:
                best, bd = col, d
    return best


def _nearest(cols: list[Column], cx: float) -> Column | None:
    return min(cols, key=lambda c: abs((c.x0 + c.x1) / 2 - cx)) if cols else None


def label_header(cols: list[Column], header_rows: list[Row]) -> str | None:
    """Assign header tokens to columns and derive year, scope, unit and key (brief 7.5)."""
    years, scopes, units, notes, texts = [], [], [], [], []
    for r in header_rows:
        for c in r.cells:
            parts = c.text.replace("’", "'").split()
            if len(c.tokens) > 1 and all(is_year(p) or is_unit(p) or _HEADER_WORDS.match(p) for p in parts):
                pieces = [(t, t["t"]) for t in c.tokens]
            else:
                pieces = [(None, c.text)]
            for tok, text in pieces:
                cell = Cell(text, list(tok["b"]) if tok else c.bbox, tok["c"] if tok else c.conf, [tok] if tok else c.tokens, role="header")
                s = text.strip().lower()
                if is_year(s):
                    years.append(cell)
                elif is_unit(s):
                    units.append(cell)
                elif re.fullmatch(r"notes?", s):
                    notes.append(cell)
                elif re.search(r"\b(group|consolidated)\b", s):
                    scopes.append(("group", cell))
                elif re.search(r"\b(company|parent)\b", s):
                    scopes.append(("company", cell))
                else:
                    texts.append(cell)
    value_cols = [c for c in cols if not c.is_note]
    for n in notes:
        col = _nearest(cols, (n.x0 + n.x1) / 2)
        if col is not None and abs((col.x0 + col.x1) / 2 - (n.x0 + n.x1) / 2) < 40:
            col.is_note = True
            col.header_cells.append(n)
    value_cols = [c for c in cols if not c.is_note]
    for col in value_cols:
        cx = (col.x0 + col.x1) / 2
        cand = [y for y in years if abs((y.x0 + y.x1) / 2 - cx) < 90]
        if cand:
            y = min(cand, key=lambda y: abs(y.x1 - col.x1) + 0.5 * abs((y.x0 + y.x1) / 2 - cx))
            col.year = int(y.text.strip())
            col.header_cells.append(y)
        cand_u = [u for u in units if abs(u.x1 - col.x1) < 30 or col.x0 - 10 <= (u.x0 + u.x1) / 2 <= col.x1 + 10]
        if cand_u:
            u = min(cand_u, key=lambda u: abs(u.x1 - col.x1))
            col.unit = canonical_unit(u.text)
            col.header_cells.append(u)
        if scopes:
            sc, cell = min(scopes, key=lambda s: abs((s[1].x0 + s[1].x1) / 2 - cx))
            col.scope = sc
            col.header_cells.append(cell)
        own = [t for t in texts if t.x1 >= col.x0 - 8 and t.x0 <= col.x1 + 8]
        if own:
            col.header_cells.extend(own)
            col.header_text = " ".join(t.text for t in sorted(own, key=lambda t: (t.bbox[1], t.x0)))
            if re.search(r"restated", col.header_text, re.I):
                col.restated = True
    # text headers that sit above several columns (statement of changes in equity) go to the nearest column
    for t in texts:
        if not any(t in c.header_cells for c in value_cols):
            col = _nearest(value_cols, (t.x0 + t.x1) / 2)
            if col is not None and abs((col.x0 + col.x1) / 2 - (t.x0 + t.x1) / 2) < 45:
                col.header_cells.append(t)
                col.header_text = (col.header_text + " " + t.text).strip()
    yrs = [c.year for c in value_cols if c.year]
    current = max(yrs) if yrs else None
    unit_counts: dict[str, int] = {}
    for col in value_cols:
        if col.unit:
            unit_counts[col.unit] = unit_counts.get(col.unit, 0) + 1
    page_unit = max(unit_counts, key=unit_counts.get) if unit_counts else None
    used: dict[str, int] = {}
    for col in value_cols:
        if col.unit is None:
            col.unit = page_unit
        if col.year is not None:
            col.period = "current" if col.year == current else "prior"
        parts = []
        if col.scope:
            parts.append(col.scope)
        if col.year:
            parts.append(str(col.year))
        elif col.header_text:
            parts.append(re.sub(r"[^a-z0-9]+", "_", col.header_text.lower()).strip("_")[:40])
        if col.restated and col.year:
            parts.append("restated")
        key = "_".join(parts) or f"col{col.index + 1}"
        col.key = key
        used[key] = used.get(key, 0) + 1
    return page_unit


def _detect_note_column(cols: list[Column], rows: list[Row]) -> None:
    if any(c.is_note for c in cols) or len(cols) < 3:
        return
    first = cols[0]
    vals = [c.num for c in first.members if c.num]
    if vals and all(v.decimals == 0 and not v.is_dash and 0 < v.value < 100 and "," not in v.raw and "(" not in v.raw for v in vals):
        nxt = cols[1]
        if len(first.members) <= len(nxt.members):
            first.is_note = True


# --------------------------------------------------------------------------- page layout


def rebuild_page(tokens: list[dict], page: int, width: float, height: float) -> PageLayout:
    raw_rows = build_rows(tokens)
    char_w = _char_width(tokens)
    rows: list[Row] = []
    for rt in raw_rows:
        cells = build_cells(rt, char_w)
        _reclassify_in_text_numbers(cells, char_w)
        rows.append(Row(cells, min(t["b"][1] for t in rt), max(t["b"][3] for t in rt)))

    # header and section detection
    for r in rows:
        nums = [c for c in r.cells if c.kind == "number" and c.x1 > width * 0.3]
        if r.y0 > height * 0.9 and len(r.cells) == 1 and r.cells[0].kind == "number" and r.cells[0].num and r.cells[0].num.decimals == 0 and abs(r.cells[0].num.value) < 1000:
            r.kind = "footer"
        elif _row_is_header(r.cells, width):
            r.kind = "header"
        elif nums:
            r.kind = "data"
        else:
            r.kind = "text"

    sections: list[Section] = []
    cur_hdr: list[Row] = []
    cur_rows: list[Row] = []
    seen_data = False
    title_lines: list[str] = []
    for r in rows:
        if r.kind == "footer":
            continue
        if r.kind == "header":
            if seen_data:
                sections.append(Section(cur_hdr, cur_rows, []))
                cur_hdr, cur_rows, seen_data = [], [], False
            elif cur_rows and not cur_hdr and not sections:
                cur_rows = []
            cur_hdr.append(r)
        else:
            if r.kind == "data":
                seen_data = True
            if not cur_hdr and not seen_data and not sections and r.kind == "text":
                title_lines.append(" ".join(c.text for c in r.cells))
            cur_rows.append(r)
    sections.append(Section(cur_hdr, cur_rows, []))
    sections = [s for s in sections if any(r.kind == "data" for r in s.rows)]

    units = []
    for s in sections:
        data_cells = [c for r in s.rows if r.kind == "data" for c in r.cells if c.kind == "number"]
        hdr_years = [c for r in s.header_rows for c in r.cells if c.kind == "number"]
        cols = cluster_columns(data_cells, width)
        _ = hdr_years
        for r in s.rows:
            if r.kind != "data":
                continue
            for c in r.cells:
                if c.kind != "number":
                    continue
                col = _assign_column(c, cols)
                c.column = col.index if col else None
        _detect_note_column(cols, s.rows)
        unit = label_header(cols, s.header_rows)
        if unit is None:
            for r in s.rows:
                for c in r.cells:
                    if is_unit(c.text) and c.kind == "text":
                        unit = canonical_unit(c.text)
                        break
                if unit:
                    break
            for col in cols:
                col.unit = col.unit or unit
        s.columns, s.unit = cols, unit
        if unit:
            units.append(unit)
        _assign_rows(s, width)
    page_unit = max(set(units), key=units.count) if units else None
    return PageLayout(page, width, height, rows, sections, page_unit, title_lines)


def _assign_rows(s: Section, width: float) -> None:
    cols = {c.index: c for c in s.columns}
    value_cols = s.value_columns()
    first_x = min((c.x0 for c in value_cols), default=width)
    for r in s.rows:
        r.values, r.label_cells, r.note, r.note_cell = {}, [], None, None
        for c in r.cells:
            col = cols.get(c.column) if c.kind == "number" and c.column is not None else None
            if col is not None and col.is_note:
                c.role = "note"
                r.note, r.note_cell = c.text, c
            elif col is not None:
                c.role = "value"
                k = col.key
                while k in r.values:
                    k += "#2"
                r.values[k] = c
            elif c.x1 <= first_x + 2 or c.kind == "text":
                if c.x0 < first_x - 5:
                    c.role = "label"
                    r.label_cells.append(c)
                else:
                    c.role = "other"
            else:
                c.role = "other"
        r.label = " ".join(c.text for c in r.label_cells).strip()
        if r.values and r.kind != "data":
            r.kind = "data"
    # join wrapped labels
    rows = s.rows
    for i in range(1, len(rows)):
        r, p = rows[i], rows[i - 1]
        if not r.values or p.values or not p.label_cells or p.kind == "consumed":
            continue
        close = r.y0 - p.y1 < (r.y1 - r.y0) * 1.2
        if not close:
            continue
        if not r.label or r.label[:1].islower() or p.label.rstrip().endswith(_CONNECTORS):
            r.label_cells = p.label_cells + r.label_cells
            r.label = (p.label + " " + r.label).strip()
            p.kind = "consumed"


_CONNECTORS = (",", "-", ":", " and", " in", " of", " for", " the", " from", " due", " to", " on", " after", " within", " less")


def section_period_columns(s: Section) -> dict[str, list[Column]]:
    out: dict[str, list[Column]] = {}
    for c in s.value_columns():
        out.setdefault(c.key, []).append(c)
    return out


def fmt_value(cell: Cell | None) -> str:
    return cell.text if cell is not None else ""


def to_markdown(layout: PageLayout, statuses: dict[int, str] | None = None) -> str:
    """Markdown tables for each section. Unverified values carry a trailing ' (?)'."""
    out = []
    for s in layout.sections:
        keys = s.column_keys()
        has_note = any(r.note for r in s.rows)
        head = ["Line item"] + (["Note"] if has_note else []) + [k + (f" ({s.unit})" if s.unit else "") for k in keys]
        out.append("| " + " | ".join(head) + " |")
        out.append("|" + "---|" * len(head))
        for r in s.rows:
            if r.kind == "consumed":
                continue
            if not r.values and not r.label:
                continue
            if not r.values and r.kind == "text" and len(r.label) > 120:
                continue
            vals = []
            for k in keys:
                c = r.values.get(k)
                v = fmt_value(c)
                if c is not None and statuses is not None and statuses.get(id(c), "").startswith("unverified"):
                    v += " (?)"
                vals.append(v)
            line = [r.label.replace("|", "/")] + ([r.note or ""] if has_note else []) + vals
            out.append("| " + " | ".join(line) + " |")
        out.append("")
    return "\n".join(out).strip()


def layout_summary(layout: PageLayout) -> dict[str, Any]:
    return {
        "page": layout.page,
        "unit": layout.unit,
        "sections": [{"columns": s.column_keys(), "rows": sum(1 for r in s.rows if r.values)} for s in layout.sections],
    }
