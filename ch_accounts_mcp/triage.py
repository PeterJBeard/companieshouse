"""Page classification, page map and notes index."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

KINDS = [
    "front",
    "strategic_report",
    "directors_report",
    "responsibilities",
    "audit_report",
    "income_statement",
    "balance_sheet",
    "equity_statement",
    "cash_flow",
    "notes",
]

# Matched against the line with everything except letters removed, so OCR that drops spaces or
# apostrophes ("Companybalancesheetasat31December2024") still classifies. Checked in this order,
# because e.g. "statement of directors' responsibilities in respect of the directors' report" must
# win over "directors' report".
_PATTERNS: list[tuple[str, list[str]]] = [
    ("responsibilities", ["statementofdirectorsresponsibilities", "directorsresponsibilitiesstatement", "statementofthedirectorsresponsibilities"]),
    ("audit_report", ["independentauditorsreport", "independentauditorreport", "reportoftheindependentauditor", "independentreportoftheauditor"]),
    ("equity_statement", ["statementofchangesinequity", "statementsofchangesinequity", "statementofchangesinshareholdersequity", "reconciliationofmovementsinshareholdersfunds"]),
    ("cash_flow", ["statementofcashflows", "statementsofcashflows", "cashflowstatement"]),
    ("income_statement", ["statementofcomprehensiveincome", "incomestatement", "profitandlossaccount", "statementofprofitorloss", "statementofprofitandloss", "statementoftotalrecognisedgainsandlosses"]),
    ("balance_sheet", ["statementoffinancialposition", "statementsoffinancialposition", "balancesheet"]),
    ("notes", ["notestothefinancialstatements", "notestotheconsolidatedfinancialstatements", "notestothegroupfinancialstatements", "notestothecompanyfinancialstatements", "notestotheaccounts", "notestothegroupaccounts", "notestotheconsolidatedaccounts", "notestothe", "notesforming", "accountingpolicies"]),
    ("strategic_report", ["strategicreport"]),
    ("directors_report", ["directorsreport", "reportofthedirectors", "reportandfinancialstatements"]),
    ("front", ["contents", "companyinformation", "officersandprofessionaladvisers", "directorsandadvisers", "officersandadvisers"]),
]

_MAX_PREFIX = 26  # letters allowed before the pattern ("consolidatedandcompany" = 22)
_MAX_LINE_LETTERS = 110


def _letters(s: str) -> str:
    return re.sub(r"[^a-z]", "", s.lower())


_RUNNING_HEADER = re.compile(r"(annual\s+)?report\s+and\s+(the\s+)?(consolidated\s+|group\s+)?(audited\s+)?(financial\s+statements|accounts)", re.I)


def is_running_header(text: str) -> bool:
    """'X Limited | Report and financial statements | 49' repeated at the top of every page."""
    t = text.strip()
    if not _RUNNING_HEADER.search(t):
        return False
    return bool(re.search(r"\d{1,3}\s*$", t)) or bool(re.search(r"\b(limited|ltd|plc|llp)\b", t, re.I))


def running_header_page_number(text: str) -> str | None:
    if not is_running_header(text):
        return None
    m = re.search(r"(?:^|[\s|Il1])(\d{1,3})\s*$", text.strip())
    if m:
        return m.group(1)
    m = re.match(r"^\s*(\d{1,3})\s*[|Il]", text)
    return m.group(1) if m else None


def classify_line(text: str) -> tuple[str, str] | None:
    """Return (kind, scope) if the line reads as a page heading."""
    if is_running_header(text):
        return None
    lt = _letters(text)
    if len(lt) < 5 or len(lt) > _MAX_LINE_LETTERS:
        return None
    for kind, pats in _PATTERNS:
        for p in pats:
            i = lt.find(p)
            if i < 0 or i > _MAX_PREFIX:
                continue
            if kind == "front" and len(lt) > 45:
                continue
            if kind == "notes" and p == "notestothe" and "statements" not in lt and "accounts" not in lt:
                continue
            if kind == "directors_report" and re.search(r"reportand(the)?(consolidated|group)?(financialstatements|accounts)", lt):
                return "front", _scope(text, lt)
            prefix = lt[:i]
            if kind != "notes" and prefix and not re.fullmatch(r"(the|consolidated|group|company|parent|and|companys|groups|of|entity|separate|statutory|interim)*", prefix):
                continue
            return kind, _scope(text, lt)
    return None


def _scope(text: str, lt: str) -> str:
    low = text.lower()
    if "consolidated" in lt or re.search(r"\bgroup\b", low):
        return "group"
    if re.search(r"\b(company|parent)\b", low) or lt.startswith("company") or lt.startswith("parentcompany"):
        return "company"
    return "unknown"


@dataclass
class PageInfo:
    page: int  # 1-based PDF page index
    kind: str = "unknown"
    scope: str = "unknown"
    heading: str | None = None
    continuation: bool = False
    printed_page: str | None = None
    text_layer: bool = False
    triage_ms: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def lines_from_tokens(tokens: list[dict]) -> list[tuple[float, str, list[dict]]]:
    """Group tokens into lines by their line id; return (y, text, tokens) sorted top to bottom."""
    by: dict[int, list[dict]] = {}
    for t in tokens:
        by.setdefault(t["l"], []).append(t)
    out = []
    for toks in by.values():
        toks.sort(key=lambda t: t["b"][0])
        out.append((min(t["b"][1] for t in toks), " ".join(t["t"] for t in toks), toks))
    out.sort(key=lambda x: (round(x[0] / 3), x[2][0]["b"][0]))
    return out


def merge_visual_lines(tokens: list[dict], tol: float = 3.0) -> list[tuple[float, str]]:
    """Join OCR lines that sit on the same baseline (headings split by OCR into pieces)."""
    lines = lines_from_tokens(tokens)
    merged: list[list] = []
    for y, text, toks in lines:
        x0 = toks[0]["b"][0]
        if merged and abs(merged[-1][0] - y) <= tol:
            merged[-1][2].append((x0, text))
        else:
            merged.append([y, None, [(x0, text)]])
    return [(m[0], " ".join(t for _, t in sorted(m[2]))) for m in merged]


def classify_page(tokens: list[dict], page_height: float) -> tuple[str, str, str] | None:
    limit = page_height * 0.35
    for y, text in merge_visual_lines([t for t in tokens if t["b"][1] < limit]):
        r = classify_line(text)
        if r:
            return r[0], r[1], text
    return None


_ROMAN = re.compile(r"^(?=[ivxlc]+$)c{0,3}(xc|xl|l?x{0,3})(ix|iv|v?i{0,3})$", re.I)


def header_page_number(tokens: list[dict], page_height: float) -> str | None:
    for _y, text in merge_visual_lines([t for t in tokens if t["b"][1] < page_height * 0.12]):
        n = running_header_page_number(text)
        if n:
            return n
    return None


def printed_page_number(bottom_tokens: list[dict], page_width: float) -> str | None:
    best = None
    for y, text, toks in lines_from_tokens(bottom_tokens):
        s = text.strip().strip("-–— ").strip()
        s = re.sub(r"^page\s+", "", s, flags=re.I)
        if re.fullmatch(r"\d{1,3}", s) or _ROMAN.match(s):
            cx = (toks[0]["b"][0] + toks[-1]["b"][2]) / 2
            score = abs(cx - page_width / 2)
            if cx > page_width * 0.8:
                score = min(score, page_width - cx)
            if best is None or score < best[0]:
                best = (score, s)
    return best[1] if best else None


def build_page_map(pages: list[PageInfo], is_group_doc: bool | None = None) -> list[PageInfo]:
    """Fill in continuation pages and resolve unknown scope."""
    prev: PageInfo | None = None
    for p in pages:
        if p.heading is None and prev is not None:
            p.kind, p.scope, p.continuation = prev.kind, prev.scope, True
        prev = p
    has_group = any(p.scope == "group" for p in pages) if is_group_doc is None else is_group_doc
    if not has_group:
        for p in pages:
            if p.kind in ("income_statement", "balance_sheet", "equity_statement", "cash_flow") and p.scope == "unknown":
                p.scope = "company"
    return pages


def page_ranges(pages: list[PageInfo]) -> list[dict]:
    out: list[dict] = []
    for p in pages:
        if out and out[-1]["kind"] == p.kind and out[-1]["scope"] == p.scope and out[-1]["end_page"] == p.page - 1:
            out[-1]["end_page"] = p.page
            if p.printed_page:
                out[-1]["printed_pages"].append(p.printed_page)
        else:
            out.append({"kind": p.kind, "scope": p.scope, "start_page": p.page, "end_page": p.page, "heading": p.heading, "printed_pages": [p.printed_page] if p.printed_page else []})
    return out


# ----------------------------------------------------------------------------- notes index

_NOTE_HEAD = re.compile(r"^(\d{1,2})\.?\s*([A-Z][A-Za-z ,'’&()\-/:]{2,70})$")


@dataclass
class NoteHeading:
    number: int
    title: str
    page: int
    y: float
    bbox: list[float] = field(default_factory=list)
    line_tokens: list[dict] = field(default_factory=list)


def note_heading_candidates(page: int, tokens: list[dict], page_width: float) -> list[NoteHeading]:
    out = []
    for y, text in merge_visual_lines(tokens):
        m = _NOTE_HEAD.match(text.strip())
        if not m:
            continue
        line_toks = [t for t in tokens if abs(t["b"][1] - y) <= 3]
        line_toks.sort(key=lambda t: t["b"][0])
        if not line_toks or line_toks[0]["b"][0] > page_width * 0.3:
            continue
        title = m.group(2).strip()
        if re.search(r"\d", title) or len(title.split()) > 9:
            continue
        x0 = min(t["b"][0] for t in line_toks)
        x1 = max(t["b"][2] for t in line_toks)
        y1 = max(t["b"][3] for t in line_toks)
        out.append(NoteHeading(int(m.group(1)), title, page, y, [x0, y, x1, y1], line_toks))
    return out


def build_notes_index(cands: list[NoteHeading]) -> list[dict]:
    """Keep the longest run of note numbers that never decreases (filters list items and table rows)."""
    cands = sorted(cands, key=lambda c: (c.page, c.y))
    n = len(cands)
    if not n:
        return []
    best = [1] * n
    prev = [-1] * n
    for i in range(n):
        for j in range(i):
            if cands[j].number <= cands[i].number <= cands[j].number + 3 and best[j] + 1 > best[i]:
                best[i], prev[i] = best[j] + 1, j
    i = max(range(n), key=lambda k: best[k])
    chain = []
    while i >= 0:
        chain.append(cands[i])
        i = prev[i]
    chain.reverse()
    notes: dict[int, dict] = {}
    for c in chain:
        e = notes.get(c.number)
        if e is None:
            notes[c.number] = {"note": c.number, "title": c.title, "start_page": c.page, "end_page": c.page, "y": c.y, "bbox": c.bbox}
        else:
            e["end_page"] = max(e["end_page"], c.page)
    ordered = [notes[k] for k in sorted(notes)]
    for a, b in zip(ordered, ordered[1:]):
        a["end_page"] = max(a["end_page"], b["start_page"])
    return ordered
