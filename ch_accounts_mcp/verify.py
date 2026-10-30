"""Arithmetic verification: casting within columns and cross-checks between statements (brief section 8)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable

from .layout import Cell, Column, PageLayout, Row, Section

VERIFIED = ("verified", "verified_rounding", "verified_after_reread", "verified_by_second_reader")
MAX_K = 12


@dataclass
class Figure:
    fid: str
    page: int
    statement: str
    scope: str
    section: int
    row_index: int
    key: str
    label: str
    value: Decimal
    decimals: int
    raw: str
    cell: Cell
    row: Row
    column: Column
    unit: str | None
    period: str | None
    year: int | None
    note: str | None = None
    status: str = "unverified"
    checks: list[dict] = field(default_factory=list)
    low_confidence: bool = False
    provisional_rounding: bool = False
    restated: bool = False

    @property
    def verified(self) -> bool:
        return self.status in VERIFIED

    def add_check(self, rule: str, detail: str, related: Iterable[str] = (), passed: bool = True) -> None:
        self.checks.append({"rule": rule, "detail": detail, "related": list(related), "passed": passed})


def figures_from_layout(layout: PageLayout, statement: str, scope: str, id_prefix: str) -> list[Figure]:
    figs: list[Figure] = []
    for si, s in enumerate(layout.sections):
        cols = {c.key: c for c in s.value_columns()}
        for ri, r in enumerate(s.rows):
            if r.kind == "consumed":
                continue
            for key, cell in r.values.items():
                if cell.num is None:
                    continue
                col = cols.get(key.split("#")[0]) or next(iter(cols.values()))
                col_scope = col.scope or scope
                figs.append(
                    Figure(
                        fid=f"{id_prefix}p{layout.page}s{si}r{ri}_{key}",
                        page=layout.page,
                        statement=statement,
                        scope=col_scope,
                        section=si,
                        row_index=ri,
                        key=key,
                        label=r.label,
                        value=cell.num.value,
                        decimals=cell.num.decimals,
                        raw=cell.text,
                        cell=cell,
                        row=r,
                        column=col,
                        unit=col.unit or s.unit or layout.unit,
                        period=col.period,
                        year=col.year,
                        note=r.note,
                        low_confidence=cell.num.low_confidence,
                        restated=col.restated,
                    )
                )
    return figs


# --------------------------------------------------------------------------- casting


@dataclass
class _Node:
    value: Decimal
    figs: list[Figure]  # the figure that represents this node (one element) or empty for synthetic


def _unit(decimals: int) -> Decimal:
    return Decimal(1).scaleb(-decimals)


def _try_collapse(stack: list[_Node], f: Figure, allow_rounding: bool) -> tuple[int, bool] | None:
    """Return (k, rounding) for the smallest k whose last-k sum equals f.value."""
    top = min(MAX_K, len(stack))
    for k in range(1, top + 1):
        members = stack[-k:]
        s = sum((n.value for n in members), Decimal(0))
        if k == 1 and f.value == 0:
            continue  # "- equals -" proves nothing
        if all(n.value == 0 for n in members) and f.value == 0:
            continue
        if s == f.value:
            return k, False
    if not allow_rounding:
        return None
    tol = _unit(f.decimals)
    for k in range(2, top + 1):
        members = stack[-k:]
        s = sum((n.value for n in members), Decimal(0))
        if abs(s - f.value) <= tol and f.value != 0:
            return k, True
    return None


def _mark(f: Figure, rounding: bool) -> None:
    if rounding:
        if not f.verified:
            f.provisional_rounding = True
        return
    if not f.verified:
        f.status = "verified"
        f.provisional_rounding = False


def cast_sequence(seq: list[Figure], direction: str = "down") -> int:
    """Collapsing-stack casting (brief 8.1). Returns number of totals found."""
    stack: list[_Node] = []
    found = 0
    for f in seq:
        hit = _try_collapse(stack, f, allow_rounding=True)
        if hit is None:
            stack.append(_Node(f.value, [f]))
            continue
        k, rounding = hit
        members = stack[-k:]
        member_figs = [m for n in members for m in n.figs]
        rule = "agrees_with" if k == 1 else ("casts_rounding" if rounding else "casts")
        if direction == "up" and k > 1:
            rule += "_forward"
        ids = [m.fid for m in member_figs]
        if k == 1:
            other = member_figs[0]
            f.add_check(rule, f"Equals {other.label or 'the line above'} {other.raw} on the same column", [other.fid])
            other.add_check(rule, f"Equals {f.label or 'the line below'} {f.raw} on the same column", [f.fid])
        else:
            what = "lines below" if direction == "up" else "lines above"
            f.add_check(rule, f"Equals the sum of the {k} {what}" + (" within one unit of rounding" if rounding else ""), ids)
            for m in member_figs:
                m.add_check(rule.replace("casts", "casts_to"), f"Part of {f.label or 'the total'} {f.raw}" + (" (rounding)" if rounding else ""), [f.fid])
        _mark(f, rounding)
        for m in member_figs:
            _mark(m, rounding)
        del stack[-k:]
        stack.append(_Node(f.value, [f]))
        found += 1
    return found


def column_sequences(figs: list[Figure]) -> dict[tuple, list[Figure]]:
    seqs: dict[tuple, list[Figure]] = {}
    for f in figs:
        seqs.setdefault((f.page, f.section, f.key.split("#")[0]), []).append(f)
    for k in seqs:
        seqs[k].sort(key=lambda f: (f.row.y0, f.cell.x1))
    return seqs


_IDENTITY_LABELS = [
    "netcurrentassets",
    "netcurrentliabilities",
    "netcurrentliabilitiesassets",
    "netcurrentassetsliabilities",
    "totalassetslesscurrentliabilities",
    "totalliabilities",
    "netassets",
    "netliabilities",
    "totalequityandliabilities",
]


def is_subtotal_identity(label: str) -> bool:
    """Fuzzy, because OCR on poor scans gives 'Net current llabltes' and 'Totalliabllities'."""
    from difflib import SequenceMatcher

    t = re.sub(r"[^a-z]", "", label.lower())
    if not 8 <= len(t) <= 40:
        return False
    return any(SequenceMatcher(None, t, c).ratio() >= 0.86 for c in _IDENTITY_LABELS)


def _is_proven_subtotal(f: Figure) -> bool:
    return f.verified and any(ch["rule"] in ("casts", "casts_forward", "casts_subtotals") for ch in f.checks)


def cast_subtotal_pairs(seq: list[Figure]) -> int:
    """Balance-sheet identities across already-proven subtotals.

    IFRS layouts put 'Total assets' between the current-assets subtotal and 'Net current liabilities',
    so the collapsing stack has consumed the subtotal it needs. Only named identity lines are tried,
    and only against pairs among the four nearest proven subtotals above them.
    """
    found = 0
    for i, f in enumerate(seq):
        if f.value == 0 or not is_subtotal_identity(f.label):
            continue
        prior = [g for g in seq[:i] if _is_proven_subtotal(g)][-4:]
        hit = None
        for a in range(len(prior)):
            for b in range(a + 1, len(prior)):
                if prior[a].value + prior[b].value == f.value:
                    hit = (prior[a], prior[b])
        if hit is None:
            continue
        f.add_check("casts_subtotals", f"Equals {hit[0].label or 'subtotal'} {hit[0].raw} + {hit[1].label or 'subtotal'} {hit[1].raw}", [hit[0].fid, hit[1].fid])
        for g in hit:
            g.add_check("casts_to", f"Part of {f.label} {f.raw}", [f.fid])
        _mark(f, False)
        found += 1
    return found


def cast_all(figs: list[Figure]) -> None:
    seqs = column_sequences(figs)
    for seq in seqs.values():
        cast_sequence(seq, "down")
        cast_sequence(list(reversed(seq)), "up")
    for seq in seqs.values():
        cast_subtotal_pairs(seq)


def cast_rows(figs: list[Figure], sections: dict[tuple[int, int], Section]) -> None:
    """Row-wise casting for multi-column statements whose last column is a total (equity statements)."""
    by_row: dict[tuple, list[Figure]] = {}
    for f in figs:
        by_row.setdefault((f.page, f.section, f.row_index), []).append(f)
    for (page, si, _ri), row in by_row.items():
        sec = sections.get((page, si))
        if sec is None:
            continue
        keys = sec.column_keys()
        if len(keys) < 3:
            continue
        total_key = next((k for k in keys if "total" in k), None)
        if total_key is None:
            continue
        tot = next((f for f in row if f.key == total_key), None)
        others = [f for f in row if f is not tot and keys.index(f.key.split("#")[0]) < keys.index(total_key)]
        if tot is None or len(others) < 2 or all(o.value == 0 for o in others):
            continue
        s = sum((o.value for o in others), Decimal(0))
        if s == tot.value:
            tot.add_check("cross_casts", f"Equals the sum of {len(others)} columns across the row", [o.fid for o in others])
            _mark(tot, False)
            for o in others:
                o.add_check("cross_casts_to", f"Part of row total {tot.raw}", [tot.fid])
                _mark(o, False)


# --------------------------------------------------------------------------- cross-checks

_RULES = [
    ("net_assets_equals_equity", "balance_sheet", r"^net (assets|liabilities)\b|^total assets less (total )?liabilities", "balance_sheet", r"^total (shareholders'?|equity)|shareholders'? funds|^total equity|^equity attributable to (owners|equity holders)|^total capital employed"),
    ("cash_agrees_cash_flow", "balance_sheet", r"^cash (at bank and in hand|and cash equivalents)$|^cash at bank$", "cash_flow", r"cash (and cash equivalents|at bank and in hand)? ?at (the )?end of|^closing cash|cash and cash equivalents at \d"),
    ("profit_agrees_equity_statement", "income_statement", r"^(profit|loss|\(loss\)/profit|profit/\(loss\)|\(loss\)|\(profit\))[ /()a-z]* for the (financial )?(year|period)", "equity_statement", r"^(profit|loss|\(loss\)/profit|profit/\(loss\))[ /()a-z]* for the (financial )?(year|period)"),
    ("tci_agrees_equity_statement", "income_statement", r"^total comprehensive (income|expense|loss)", "equity_statement", r"^total comprehensive (income|expense|loss)"),
    ("profit_agrees_cash_flow", "income_statement", r"^(profit|loss|\(loss\)/profit|profit/\(loss\))[ /()a-z]* for the (financial )?(year|period)", "cash_flow", r"^(profit|loss|\(loss\)/profit|profit/\(loss\))[ /()a-z]* for the (financial )?(year|period)"),
]


def _norm_label(s: str) -> str:
    s = s.lower().replace("’", "'")
    s = re.sub(r"\s+", " ", s)
    return s.strip(" :.")


def _same_period(a: Figure, b: Figure) -> bool:
    if a.year and b.year:
        return a.year == b.year
    return a.period is not None and a.period == b.period


def cross_check(figs: list[Figure]) -> list[dict]:
    results = []
    for rule, st_a, pat_a, st_b, pat_b in _RULES:
        ra, rb = re.compile(pat_a), re.compile(pat_b)
        A = [f for f in figs if f.statement == st_a and ra.search(_norm_label(f.label))]
        B = [f for f in figs if f.statement == st_b and rb.search(_norm_label(f.label))]
        for a in A:
            for b in B:
                if a is b or a.scope != b.scope:
                    continue
                if st_b == "equity_statement":
                    if not (b.year == a.year or (a.year and str(a.year) in b.label) or b.year is None):
                        continue
                elif not _same_period(a, b):
                    continue
                if a.value == b.value and a.value != 0:
                    a.add_check(rule, f"Agrees to {b.statement.replace('_', ' ')} '{b.label}' {b.raw} (page {b.page})", [b.fid])
                    b.add_check(rule, f"Agrees to {a.statement.replace('_', ' ')} '{a.label}' {a.raw} (page {a.page})", [a.fid])
                    if a.page != b.page or a.statement == b.statement:
                        _mark(a, False)
                        _mark(b, False)
                    results.append({"rule": rule, "passed": True, "a": a.fid, "b": b.fid, "value": str(a.value)})
    # balance-sheet equity lines agree to the closing row of the equity statement
    bs = [f for f in figs if f.statement == "balance_sheet"]
    soce_closing = [f for f in figs if f.statement == "equity_statement" and re.match(r"^(at|balance at|as at)\b", _norm_label(f.label))]
    for f in bs:
        if f.value == 0 or not f.year:
            continue
        for g in soce_closing:
            if g.scope != f.scope or str(f.year) not in g.label or not re.search(r"31|30|29|28|1 |2 |3 ", g.label):
                continue
            if g.value == f.value and _label_overlap(f.label, g.column.header_text) >= 0.5:
                f.add_check("agrees_equity_statement", f"Agrees to equity statement '{g.label}' column '{g.column.header_text}' {g.raw} (page {g.page})", [g.fid])
                g.add_check("agrees_balance_sheet", f"Agrees to balance sheet '{f.label}' {f.raw} (page {f.page})", [f.fid])
                _mark(f, False)
                _mark(g, False)
    # same line item, same period and scope, read independently on two different pages
    by_label: dict[tuple, list[Figure]] = {}
    for f in figs:
        if f.value == 0 or len(f.label) < 6:
            continue
        by_label.setdefault((_norm_label(f.label), f.scope, f.year or f.period), []).append(f)
    for group in by_label.values():
        for i, a in enumerate(group):
            for b in group[i + 1 :]:
                if a.page != b.page and a.value == b.value:
                    a.add_check("agrees_elsewhere", f"Same figure for '{b.label}' on page {b.page}", [b.fid])
                    b.add_check("agrees_elsewhere", f"Same figure for '{a.label}' on page {a.page}", [a.fid])
                    _mark(a, False)
                    _mark(b, False)
    return results


def _label_overlap(a: str, b: str) -> float:
    wa = set(re.findall(r"[a-z]{3,}", a.lower()))
    wb = set(re.findall(r"[a-z]{3,}", b.lower()))
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / min(len(wa), len(wb))


def finalise(figs: list[Figure], rounding_confirmed: set[str] | None = None) -> None:
    """Resolve provisional rounding matches: only accepted when a second reading confirmed the digits."""
    for f in figs:
        if not f.verified and f.provisional_rounding and rounding_confirmed is not None and f.fid in rounding_confirmed:
            f.status = "verified_rounding"


def summary(figs: list[Figure]) -> dict:
    counts: dict[str, int] = {}
    for f in figs:
        counts[f.status] = counts.get(f.status, 0) + 1
    total = len(figs)
    ver = sum(v for k, v in counts.items() if k in VERIFIED)
    return {"figures": total, "verified": ver, "unverified": total - ver, "share_verified": round(ver / total, 4) if total else None, "by_status": counts}
