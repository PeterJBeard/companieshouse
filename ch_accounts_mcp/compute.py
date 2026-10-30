"""Server-side metrics with cited inputs and an explicit formula (brief section 15.8)."""

from __future__ import annotations

import ast
import operator
import re
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from .citations import citation_id
from .layout import unit_multiplier
from .pipeline import Document, crop_url, viewer_url

# input name -> (statement, label regex, sign handling)
_INPUTS: dict[str, tuple[str, str]] = {
    "revenue": ("income_statement", r"^(group )?(revenue|turnover|gross revenue|sales)$|^revenue from contracts|^turnover\b"),
    "gross_profit": ("income_statement", r"^gross (profit|loss|\(loss\)/profit|profit/\(loss\))$"),
    "operating_profit": ("income_statement", r"^(group )?operating (profit|loss|\(loss\)/profit|profit/\(loss\))$|^operating (profit|loss) before"),
    "finance_costs": ("income_statement", r"^(finance costs|interest payable( and similar (charges|expenses))?|finance expense|interest expense)"),
    "profit_after_tax": ("income_statement", r"^(profit|loss|\(loss\)/profit|profit/\(loss\))[ /()a-z]* for the (financial )?(year|period)"),
    "depreciation": ("cash_flow", r"^depreciation\b"),
    "amortisation": ("cash_flow", r"^amortisation\b"),
    "net_assets": ("balance_sheet", r"^net (assets|liabilities)$"),
    "total_equity": ("balance_sheet", r"^total (equity|shareholders'? funds)|^shareholders'? funds|^equity attributable to (owners|equity holders) of the (parent|company)"),
    "current_assets": ("balance_sheet", r"^total current assets$|^current assets$"),
    "net_current_assets": ("balance_sheet", r"^net current (assets|liabilities|\(liabilities\)/assets|assets/\(liabilities\))"),
    "current_liabilities": ("balance_sheet", r"^total current liabilities$|^creditors:? amounts falling due within one year$"),
    "cash": ("balance_sheet", r"^cash (at bank and in hand|and cash equivalents|and bank balances)$|^cash at bank$"),
    "borrowings": ("balance_sheet", r"borrowings|^bank (loans|overdrafts?)|^loans\b|loan notes|^interest[- ]bearing loans|^debt\b"),
    "lease_liabilities": ("balance_sheet", r"^lease liabilit|^obligations under (finance leases|leases)"),
}

METRICS: dict[str, dict[str, Any]] = {
    "net_assets": {"definition": "Net assets as stated on the balance sheet.", "inputs": ["net_assets"], "expr": "net_assets", "ratio": False},
    "gross_margin": {"definition": "Gross profit divided by revenue (turnover).", "inputs": ["gross_profit", "revenue"], "expr": "gross_profit / revenue", "ratio": True},
    "operating_margin": {"definition": "Operating profit divided by revenue (turnover).", "inputs": ["operating_profit", "revenue"], "expr": "operating_profit / revenue", "ratio": True},
    "net_margin": {"definition": "Profit for the year divided by revenue (turnover).", "inputs": ["profit_after_tax", "revenue"], "expr": "profit_after_tax / revenue", "ratio": True},
    "current_ratio": {
        "definition": "Current assets divided by current liabilities (both as positive amounts).",
        "inputs": ["current_assets", "current_liabilities"],
        "expr": "abs(current_assets) / abs(current_liabilities)",
        "ratio": False,
        # FRS 102 formats leave the current assets subtotal unlabelled
        "alt": {"inputs": ["net_current_assets", "current_liabilities"], "expr": "(net_current_assets + abs(current_liabilities)) / abs(current_liabilities)", "note": "Current assets taken as net current assets plus creditors due within one year, because the current assets subtotal is unlabelled."},
    },
    "net_debt": {
        "definition": "Balance-sheet borrowings (all lines labelled borrowings, loans, loan notes or overdrafts, current and non-current) plus lease liabilities, less cash. Definitions of net debt vary between firms; supply a custom definition if yours differs.",
        "inputs": ["borrowings*", "lease_liabilities*", "cash"],
        "expr": "abs(borrowings) + abs(lease_liabilities) - abs(cash)",
        "ratio": False,
    },
    "ebitda": {
        "definition": "Operating profit plus depreciation and amortisation as added back in the cash flow statement. EBITDA definitions vary; this excludes exceptional items only if the filing does.",
        "inputs": ["operating_profit", "depreciation*", "amortisation*"],
        "expr": "operating_profit + abs(depreciation) + abs(amortisation)",
        "ratio": False,
    },
    "interest_cover": {"definition": "Operating profit divided by finance costs.", "inputs": ["operating_profit", "finance_costs"], "expr": "operating_profit / abs(finance_costs)", "ratio": False},
    "gearing": {"definition": "Net debt (as defined for net_debt) divided by total equity.", "inputs": ["borrowings*", "lease_liabilities*", "cash", "total_equity"], "expr": "(abs(borrowings) + abs(lease_liabilities) - abs(cash)) / total_equity", "ratio": True},
    "revenue_growth": {"definition": "Current-year revenue divided by prior-year revenue, minus one.", "inputs": ["revenue", "revenue@prior"], "expr": "revenue / revenue_prior - 1", "ratio": True},
}

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv, ast.USub: operator.neg}


def _eval(expr: str, env: dict[str, Decimal]) -> Decimal:
    def ev(n: ast.AST) -> Decimal:
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.UnaryOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.operand))
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return Decimal(str(n.value))
        if isinstance(n, ast.Name):
            if n.id not in env:
                raise ValueError(f"unknown input {n.id}")
            return env[n.id]
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "abs" and len(n.args) == 1:
            return abs(ev(n.args[0]))
        raise ValueError("only + - * / abs() and citation ids are allowed in a definition")

    return ev(ast.parse(expr, mode="eval"))


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower().replace("’", "'")).strip(" :.")


def _figure_citations(doc: Document) -> list[dict]:
    doc.primary_statements()
    return [c for c in doc.citations().values() if c.get("type") == "figure"]


def _pick_scope(figs: list[dict], scope: str | None) -> str:
    if scope:
        return scope
    scopes = {f["scope"] for f in figs}
    return "group" if "group" in scopes else ("company" if "company" in scopes else next(iter(scopes), "unknown"))


def _periods(figs: list[dict], scope: str) -> tuple[int | None, int | None]:
    years = sorted({f["year"] for f in figs if f["scope"] == scope and f.get("year")}, reverse=True)
    return (years[0] if years else None, years[1] if len(years) > 1 else None)


def _find(figs: list[dict], name: str, scope: str, year: int | None, period: str) -> list[dict]:
    st, pat = _INPUTS[name]
    rx = re.compile(pat)
    out = [
        f
        for f in figs
        if f["statement"] == st
        and f["scope"] == scope
        and rx.search(_norm(f["label"] or ""))
        and ((year is not None and f.get("year") == year) or (year is None and f.get("period") == period))
    ]
    out.sort(key=lambda f: (f["pdf_page"], f["row_bbox"][1]))
    # one figure per distinct row: the same row can be read on two pages of the same statement
    seen, uniq = set(), []
    for f in out:
        k = (_norm(f["label"]), f["value_exact"], f.get("note"))
        if k not in seen:
            seen.add(k)
            uniq.append(f)
    return uniq


def _resolve(names: list[str], figs: list[dict], sc: str, yr: int | None, prior_yr: int | None) -> tuple[dict, list, list]:
    env: dict[str, Decimal] = {}
    used: list[tuple[str, list[dict]]] = []
    missing: list[str] = []
    starred = [n for n in names if n.endswith("*")]
    for raw_name in names:
        optional_sum = raw_name.endswith("*")
        name = raw_name.rstrip("*")
        want_prior = name.endswith("@prior")
        base = name.replace("@prior", "")
        var = base + ("_prior" if want_prior else "")
        hits = _find(figs, base, sc, prior_yr if want_prior else yr, "prior" if want_prior else "current")
        if not hits:
            if optional_sum:
                env[var] = Decimal(0)
                used.append((var, []))
                continue
            missing.append(var)
            continue
        if optional_sum:
            env[var] = sum((abs(Decimal(h["value_exact"])) * unit_multiplier(h.get("unit")) for h in hits), Decimal(0))
            used.append((var, hits))
        else:
            env[var] = Decimal(hits[0]["value_exact"]) * unit_multiplier(hits[0].get("unit"))
            used.append((var, hits[:1]))
    if starred and all(not ch for v, ch in used if v in {n.rstrip("*").replace("@prior", "") for n in starred}):
        # every optional component is absent: the face of the statements does not carry these lines
        missing += [n.rstrip("*") for n in starred]
    return env, used, missing


def compute(doc: Document, metric: str, definition: str | None = None, scope: str | None = None, year: int | None = None) -> dict:
    figs = _figure_citations(doc)
    sc = _pick_scope(figs, scope)
    cur, prior = _periods(figs, sc)
    yr = year or cur
    prior_yr = (yr - 1) if yr else prior
    by_cid = {c["citation_id"]: c for c in doc.citations().values()}
    env: dict[str, Decimal] = {}
    used: list[tuple[str, list[dict]]] = []
    missing: list[str] = []
    expr: str
    ratio: bool
    if definition and re.search(r"c_[0-9a-f]{12}", definition):
        expr = definition
        for cid in sorted(set(re.findall(r"c_[0-9a-f]{12}", definition))):
            c = by_cid.get(cid)
            if c is None:
                return {"error": f"citation {cid} is not part of this document"}
            if c.get("type") == "quote":
                return {"error": f"citation {cid} is a quote, not a figure"}
            env[cid] = Decimal(c["value_exact"]) * unit_multiplier(c.get("unit"))
            used.append((cid, [c]))
        ratio = "/" in definition
        defn = f"Custom definition supplied by the caller: {definition}"
        mname = metric or "custom"
    else:
        m = METRICS.get(metric.lower().strip().replace(" ", "_"))
        if m is None:
            return {"error": f"unknown metric {metric!r}; use one of {', '.join(METRICS)} or pass a definition written over citation ids, for example 'c_abc123def456 - c_0123456789ab'"}
        expr, ratio, defn, mname = m["expr"], m["ratio"], m["definition"] + (f" Note from caller: {definition}" if definition else ""), metric
        env, used, missing = _resolve(m["inputs"], figs, sc, yr, prior_yr)
        if missing and m.get("alt"):
            env2, used2, missing2 = _resolve(m["alt"]["inputs"], figs, sc, yr, prior_yr)
            if not missing2:
                env, used, missing, expr = env2, used2, [], m["alt"]["expr"]
                defn += " " + m["alt"]["note"]
    if missing:
        return {
            "error": f"could not find {', '.join(missing)} for {sc} {yr or ''} on the primary statements",
            "metric": mname,
            "definition": defn,
            "hint": "Pass a definition written over citation ids from get_primary_statements, for example 'c_aaa + c_bbb - c_ccc'.",
        }
    try:
        value = _eval(expr, env)
    except ZeroDivisionError:
        return {"error": "division by zero", "metric": mname}
    except ValueError as e:
        return {"error": str(e), "metric": mname}
    parts = []
    inputs = []
    any_unverified = False
    for var, chosen in used:
        if not chosen:
            parts.append(f"{var} 0 (no line found)")
            continue
        for c in chosen:
            any_unverified |= c["status"] != "extracted_verified"
            parts.append(f"{c['label']} {c['raw']} [{c['citation_id']}]")
            inputs.append({"input": var, "citation_id": c["citation_id"], "label": c["label"], "raw": c["raw"], "value": c["value"], "unit": c.get("unit"), "pdf_page": c["pdf_page"], "printed_page": c.get("printed_page"), "provenance": c["status"]})
    units = {c.get("unit") for _, ch in used for c in ch}
    times = mname in ("current_ratio", "interest_cover")
    unit = "%" if ratio else ("x" if times else "£")
    if not ratio and not times and len(units) == 1 and next(iter(units)):
        # show the result in the filing's own unit, as the inputs are
        unit = next(iter(units))
        value = value / unit_multiplier(unit)
    shown = value * 100 if ratio else value
    in_dec = max((len(c["value_exact"].split(".")[1]) if "." in c["value_exact"] else 0 for _, ch in used for c in ch), default=0)
    q = Decimal("0.1") if ratio else (Decimal("0.01") if times else Decimal(1).scaleb(-in_dec))
    shown = shown.quantize(q, rounding=ROUND_HALF_UP)
    sha = doc.sha
    cid = citation_id(sha, "computed", mname, expr, *(c["citation_id"] for _, ch in used for c in ch))
    formula = f"{mname} {shown}{'%' if ratio else ''} = {expr}  where " + "; ".join(parts)
    cit = {
        "citation_id": cid,
        "type": "computed",
        "doc_sha256": sha,
        "company_number": doc.meta.get("company_number"),
        "metric": mname,
        "definition": defn,
        "formula": formula,
        "expression": expr,
        "value": float(value),
        "value_exact": str(value),
        "display": f"{shown}{'%' if ratio else ''}",
        "unit": unit,
        "is_ratio": ratio,
        "scope": sc,
        "year": yr,
        "inputs": inputs,
        "inputs_unverified": any_unverified,
        "status": "computed",
        "spans": [],
        "checks": [],
        "viewer_url": viewer_url(sha, inputs[0]["citation_id"]) if inputs else None,
        "crop_url": None,
    }
    doc._store_citations({cid: cit})
    return cit
