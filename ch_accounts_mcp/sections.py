"""Notes, audit report, named sections and search, all returned verbatim with quote citations."""

from __future__ import annotations

import math
import re
import time
from collections import Counter
from difflib import SequenceMatcher
from typing import Any

from . import ocr
from .pipeline import Document, _ms

TOPIC_SYNONYMS = {
    "borrowings": ["borrowings", "loans", "bank loans", "interest bearing", "interest-bearing", "debt", "financing", "loan notes", "overdraft"],
    "leases": ["leases", "lease", "right-of-use", "right of use", "leasing"],
    "related party": ["related party", "related parties", "related party transactions", "controlling party"],
    "tax": ["tax", "taxation", "income tax", "tax on profit", "deferred tax"],
    "deferred tax": ["deferred tax", "deferred taxation"],
    "revenue": ["revenue", "turnover", "sales"],
    "employees": ["employees", "staff costs", "employee", "directors' remuneration", "directors remuneration", "employee information"],
    "directors remuneration": ["directors' remuneration", "directors remuneration", "directors emoluments", "directors' emoluments"],
    "intangible assets": ["intangible assets", "goodwill", "intangible"],
    "tangible assets": ["tangible assets", "property, plant and equipment", "tangible fixed assets", "fixed assets"],
    "investments": ["investments", "fixed asset investments", "investments in subsidiaries", "subsidiaries"],
    "inventories": ["inventories", "stocks", "stock"],
    "debtors": ["debtors", "trade and other receivables", "receivables"],
    "creditors": ["creditors", "trade and other payables", "payables"],
    "provisions": ["provisions", "provisions for liabilities"],
    "pensions": ["pensions", "pension", "retirement benefit", "post-employment benefits", "defined benefit"],
    "share capital": ["share capital", "called up share capital", "called-up share capital"],
    "reserves": ["reserves"],
    "commitments": ["commitments", "capital commitments", "financial commitments"],
    "contingent liabilities": ["contingent liabilities", "contingencies", "guarantees"],
    "post balance sheet events": ["post balance sheet events", "subsequent events", "events after the reporting period", "events after the balance sheet date"],
    "accounting policies": ["accounting policies", "principal accounting policies", "significant accounting policies", "basis of preparation"],
    "operating profit": ["operating profit", "operating loss", "profit before tax", "profit on ordinary activities"],
    "dividends": ["dividends", "dividend"],
    "cash": ["cash", "cash and cash equivalents", "net debt", "analysis of net debt", "analysis of changes in net debt"],
    "net debt": ["net debt", "analysis of net debt", "analysis of changes in net debt", "reconciliation of net debt"],
    "auditor remuneration": ["auditor's remuneration", "auditors remuneration", "auditor remuneration", "fees payable to the company's auditor"],
}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", s.lower().replace("’", "'").replace("'", "")).strip()


def _score_title(title: str, topic: str) -> float:
    t, q = _norm(title), _norm(topic)
    if not t or not q:
        return 0.0
    if q == t:
        return 1.0
    terms = [q] + [_norm(x) for k, v in TOPIC_SYNONYMS.items() if _norm(k) == q or q in [_norm(y) for y in v] for x in v]
    best = 0.0
    padded = f" {t} "
    for term in terms:
        if term and (f" {term} " in padded or f" {t} " in f" {term} "):
            best = max(best, 0.9 if term == q else 0.8)
        best = max(best, SequenceMatcher(None, t, term).ratio() * 0.85)
    return best


def _content_match(doc: Document, idx: list[dict], topic: str) -> tuple[dict | None, list[dict]]:
    """Fallback when no note title matches: the note whose text mentions the topic most."""
    q = _norm(topic)
    terms = {q} | {_norm(x) for k, v in TOPIC_SYNONYMS.items() if _norm(k) == q or q in [_norm(y) for y in v] for x in v}
    scores = []
    for i, e in enumerate(idx):
        nxt = idx[i + 1] if i + 1 < len(idx) else None
        text = ""
        for p, (y0, y1) in _note_ranges(e, nxt, doc).items():
            toks = doc._cached_tokens(p) or []
            text += " " + " ".join(t["t"] for t in toks if y0 <= t["b"][1] < y1)
        nt = f" {_norm(text)} "
        hits = sum(nt.count(f" {term} ") for term in terms if term)
        if re.search(r"accounting policies|judgements|estimat|basis of preparation", e["title"], re.I):
            hits = hits * 0.25  # the policies note mentions every topic; prefer the note that carries the numbers
        if hits:
            scores.append((hits, e))
    scores.sort(key=lambda x: -x[0])
    if not scores:
        return None, []
    return scores[0][1], [{"note": e["note"], "title": e["title"], "pdf_page": e["start_page"], "mentions": h} for h, e in scores[1:6]]


def _is_numeric_line(ln: dict) -> bool:
    toks = ln.get("tokens") or []
    if not toks:
        return False
    num = sum(1 for t in toks if re.fullmatch(r"[\d,.()\-–—]+", t["t"]) and re.search(r"\d", t["t"]))
    return num >= 2 and num / len(toks) >= 0.3


def _note_ranges(entry: dict, nxt: dict | None, doc: Document) -> dict[int, tuple[float, float]]:
    out = {}
    end_page = nxt["start_page"] if nxt else entry["end_page"]
    for p in range(entry["start_page"], end_page + 1):
        _w, h = doc.page_size(p)
        y0 = entry["y"] - 1 if p == entry["start_page"] else 0.0
        y1 = nxt["y"] - 1 if nxt and p == nxt["start_page"] else h + 1
        if y1 > y0:
            out[p] = (y0, y1)
    return out


def get_note(doc: Document, topic: str) -> dict:
    t0 = time.perf_counter()
    pm, hit, _ = doc.page_map()
    idx = doc.ensure_full_notes_index()
    timings = {"notes_index": _ms(t0)}
    if not idx:
        return {"error": "no notes index could be built for this document", "timings_ms": timings, "cache_hit": hit}
    entry = None
    alternatives: list[dict] = []
    m = re.fullmatch(r"\s*(?:note\s*)?(\d{1,2})\s*", topic, re.I)
    if m:
        entry = next((e for e in idx if e["note"] == int(m.group(1))), None)
    else:
        scored = sorted(((max(_score_title(e["title"], topic), 0), e) for e in idx), key=lambda x: -x[0])
        if scored and scored[0][0] >= 0.75:
            entry = scored[0][1]
            alternatives = [{"note": e["note"], "title": e["title"], "pdf_page": e["start_page"], "score": round(s, 2)} for s, e in scored[1:6] if s >= 0.6]
        else:
            entry, alternatives = _content_match(doc, idx, topic)
            if entry is not None:
                alternatives.insert(0, {"matched_by": "note text mentions the topic; no note title matched"})
    if entry is None:
        return {
            "error": f"no note matched {topic!r}",
            "notes_index": [{"note": e["note"], "title": e["title"], "pdf_page": e["start_page"]} for e in idx],
            "timings_ms": timings,
            "cache_hit": hit,
        }
    i = idx.index(entry)
    nxt = idx[i + 1] if i + 1 < len(idx) else None
    ranges = _note_ranges(entry, nxt, doc)
    t1 = time.perf_counter()
    y_from = {p: r[0] for p, r in ranges.items()}
    y_to = {p: r[1] for p, r in ranges.items()}
    paras = [p for p in doc.paragraphs(sorted(ranges), section=f"note {entry['note']}", y_from=y_from, y_to=y_to)]
    timings["text"] = _ms(t1)
    t2 = time.perf_counter()
    tables = doc.tables(ranges, "note", "unknown")
    timings["tables"] = _ms(t2)
    numeric_text = {(t["pdf_page"]) for t in tables}
    text = [p for p in paras if not (p["pdf_page"] in numeric_text and _looks_tabular(p["text"]))]
    pages = sorted(ranges)
    return {
        "doc_id": doc.sha,
        "note": entry["note"],
        "title": entry["title"],
        "pdf_pages": pages,
        "printed_pages": [pm["pages"][p - 1].get("printed_page") for p in pages],
        "text": text,
        "tables": tables,
        "alternatives": alternatives,
        "guidance": "Text is verbatim OCR of the filing; quote it rather than paraphrasing. Table figures marked unverified did not cast and must be treated as uncertain.",
        "timings_ms": timings,
        "cache_hit": hit,
    }


def _looks_tabular(text: str) -> bool:
    toks = text.split()
    if not toks:
        return False
    num = sum(1 for t in toks if re.search(r"\d", t) and re.fullmatch(r"[£$€]?[\d,.()\-–—]+", t))
    return num >= 3 and num / len(toks) >= 0.3


# --------------------------------------------------------------------------- audit report

_OPINION_PATTERNS = [
    ("disclaimer", r"\bdisclaimer of opinion\b|\bwe do not express an opinion\b"),
    ("adverse", r"\badverse opinion\b"),
    ("qualified", r"\bqualified opinion\b|\bexcept for the (effects?|possible effects?)\b"),
]


def _find_para(paras: list[dict], pattern: str) -> dict | None:
    rx = re.compile(pattern, re.I)
    return next((p for p in paras if rx.search(p["text"])), None)


def get_audit_report(doc: Document) -> dict:
    t0 = time.perf_counter()
    pm, hit, _ = doc.page_map()
    pages = [p["page"] for p in pm["pages"] if p["kind"] == "audit_report"]
    if not pages:
        return {"error": "no independent auditor's report was found in the page map", "timings_ms": {}, "cache_hit": hit}
    paras = doc.paragraphs(pages, section="audit_report")
    timings = {"text": _ms(t0)}
    full = " ".join(p["text"] for p in paras)

    opinion = None
    opinion_para = None
    for kind, pat in _OPINION_PATTERNS:
        p = _find_para(paras, pat)
        if p and not re.search(r"\b(basis for|not) (a )?(qualified|adverse)", p["text"], re.I):
            opinion, opinion_para = kind, p
            break
    if opinion is None:
        p = _find_para(paras, r"\bin our opinion\b")
        if p:
            opinion, opinion_para = "unmodified", p
    mu = _find_para(paras, r"material uncertainty (related|relating) to going concern")
    if mu and re.search(r"\bnot identified any material uncertaint", mu["text"], re.I):
        mu_flag = False
    else:
        mu_flag = mu is not None and not re.search(r"\bno material uncertaint", mu["text"], re.I)
    eom = _find_para(paras, r"^emphasis of matter\b|\bemphasis of matter\b")
    kam = _find_para(paras, r"^key audit matters\b")
    ssa_para = _find_para(paras, r"senior statutory auditor")
    ssa = None
    firm = None
    if ssa_para:
        m = re.search(r"([A-Z][A-Za-z.'\- ]{2,60}?)\s*\(?\s*Senior [Ss]tatutory [Aa]uditor", ssa_para["text"])
        if m:
            ssa = m.group(1).strip(" ,(")
        m2 = re.search(r"for and on behalf of\s+(.+?)(?:,|\s+Statutory Auditors?|\s+Chartered Accountants|$)", ssa_para["text"], re.I)
        if m2:
            firm = m2.group(1).strip()
    if firm is None:
        p = _find_para(paras, r"for and on behalf of")
        if p:
            m2 = re.search(r"for and on behalf of\s+(.+?)(?:,|\s+Statutory Auditors?|\s+Chartered Accountants|$)", p["text"], re.I)
            firm = m2.group(1).strip() if m2 else None
    if firm is None:
        m3 = re.search(r"\b([A-Z][A-Za-z&' ]{1,40}(?:LLP|LIMITED|Limited|Ltd))\s*,?\s*(?:Statutory Auditor|Chartered Accountants)", full)
        firm = m3.group(1).strip() if m3 else None

    def cite(p: dict | None) -> dict | None:
        return {"citation_id": p["citation_id"], "pdf_page": p["pdf_page"], "printed_page": p["printed_page"], "verbatim": p["text"], "provenance": "quoted"} if p else None

    return {
        "doc_id": doc.sha,
        "pdf_pages": pages,
        "printed_pages": [pm["pages"][p - 1].get("printed_page") for p in pages],
        "auditor_firm": firm,
        "auditor_firm_source": cite(ssa_para or _find_para(paras, r"for and on behalf of")),
        "senior_statutory_auditor": ssa,
        "opinion_type": opinion,
        "opinion_source": cite(opinion_para),
        "emphasis_of_matter": eom is not None,
        "emphasis_of_matter_source": cite(eom),
        "material_uncertainty_going_concern": mu_flag,
        "material_uncertainty_source": cite(mu),
        "key_audit_matters_present": kam is not None,
        "key_audit_matters_source": cite(kam),
        "text": paras,
        "guidance": "Fields such as opinion_type are read from the verbatim text by pattern; always quote the cited paragraph, and confirm against the page, before relying on them.",
        "timings_ms": timings,
        "cache_hit": hit,
    }


# --------------------------------------------------------------------------- named sections

SECTIONS = ("strategic_report", "directors_report", "accounting_policies", "going_concern", "post_balance_sheet_events")


def _is_heading(text: str) -> bool:
    words = text.split()
    return 0 < len(words) <= 9 and not text.rstrip().endswith((".", ",", ";")) and text[:1].isupper()


def _heading_block(paras: list[dict], pattern: str) -> list[dict]:
    rx = re.compile(pattern, re.I)
    for i, p in enumerate(paras):
        head = p["text"].split(".")[0]
        if rx.search(head[:80]) and (_is_heading(p["text"]) or rx.match(p["text"])):
            block = [p]
            for q in paras[i + 1 :]:
                if _is_heading(q["text"]) and not rx.search(q["text"]) and len(block) > 1:
                    break
                block.append(q)
                if len(block) > 25:
                    break
            return block
    return []


def get_section(doc: Document, section: str) -> dict:
    t0 = time.perf_counter()
    key = section.lower().strip().replace(" ", "_").replace("'", "")
    if key not in SECTIONS:
        return {"error": f"unknown section {section!r}; use one of {', '.join(SECTIONS)}"}
    pm, hit, _ = doc.page_map()
    kinds = {"strategic_report": "strategic_report", "directors_report": "directors_report"}
    if key in kinds:
        pages = [p["page"] for p in pm["pages"] if p["kind"] == kinds[key]]
        if not pages:
            return {"error": f"no {key.replace('_', ' ')} found in the page map", "cache_hit": hit, "timings_ms": {}}
        paras = doc.paragraphs(pages, section=key)
        return _section_out(doc, pm, key, paras, hit, {"text": _ms(t0)})
    if key in ("accounting_policies", "post_balance_sheet_events"):
        topic = "accounting policies" if key == "accounting_policies" else "post balance sheet events"
        note = get_note(doc, topic)
        if "error" not in note:
            note["section"] = key
            return note
        if key == "post_balance_sheet_events":
            return _search_heading(doc, pm, key, r"post balance sheet events|subsequent events|events after the (reporting period|balance sheet date)", hit, t0)
        return note
    # going concern: a heading inside accounting policies or the directors' report
    return _search_heading(doc, pm, key, r"^going concern|going concern$|basis of preparation.{0,20}going concern", hit, t0)


def _search_heading(doc: Document, pm: dict, key: str, pattern: str, hit: bool, t0: float) -> dict:
    idx = doc.ensure_full_notes_index()
    pol = next((e for e in idx if re.search(r"accounting policies|basis of preparation", e["title"], re.I)), None)
    order: list[int] = []
    if pol:
        nxt = next((e for e in idx if e["note"] > pol["note"]), None)
        order += list(range(pol["start_page"], (nxt["start_page"] if nxt else pol["end_page"]) + 1))
    order += [p["page"] for p in pm["pages"] if p["kind"] in ("directors_report", "strategic_report") and p["page"] not in order]
    order += [p["page"] for p in pm["pages"] if p["kind"] == "notes" and p["page"] not in order]
    for i in range(0, len(order), 4):
        chunk = order[i : i + 4]
        paras = doc.paragraphs(chunk, section=key)
        block = _heading_block(paras, pattern)
        if block:
            return _section_out(doc, pm, key, block, hit, {"text": _ms(t0)})
    return {"error": f"no {key.replace('_', ' ')} heading found", "cache_hit": hit, "timings_ms": {"text": _ms(t0)}}


def _section_out(doc: Document, pm: dict, key: str, paras: list[dict], hit: bool, timings: dict) -> dict:
    pages = sorted({p["pdf_page"] for p in paras})
    return {
        "doc_id": doc.sha,
        "section": key,
        "pdf_pages": pages,
        "printed_pages": [pm["pages"][p - 1].get("printed_page") for p in pages],
        "text": paras,
        "guidance": "Verbatim OCR text with page references. Quote it; do not paraphrase as if quoting.",
        "timings_ms": timings,
        "cache_hit": hit,
    }


# --------------------------------------------------------------------------- search

_WORD = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")


def search_document(doc: Document, query: str, limit: int = 10, ocr_all: bool = True) -> dict:
    t0 = time.perf_counter()
    pm, hit, _ = doc.page_map()
    n = pm["page_count"]
    pages = list(range(1, n + 1))
    timings: dict[str, float] = {}
    if ocr_all:
        _, t = doc.tokens(pages, ocr.FULL_DPI)
        timings.update(t)
    else:
        pages = [p for p in pages if doc._cached_tokens(p) is not None]
    t1 = time.perf_counter()
    from .pipeline import group_paragraphs, lines_from_page_tokens

    docs: list[tuple[int, list[dict], str]] = []
    for p in pages:
        toks = doc._cached_tokens(p) or []
        for para in group_paragraphs(lines_from_page_tokens(toks)):
            text = " ".join(ln["text"] for ln in para)
            docs.append((p, para, text))
    q_terms = [w for w in _WORD.findall(query.lower()) if len(w) > 1]
    if not q_terms or not docs:
        return {"doc_id": doc.sha, "query": query, "results": [], "timings_ms": timings, "cache_hit": hit}
    df: Counter = Counter()
    tfs = []
    for _p, _para, text in docs:
        words = _WORD.findall(text.lower())
        tf = Counter(words)
        tfs.append((tf, len(words)))
        for w in set(words):
            df[w] += 1
    N = len(docs)
    avg = sum(l for _, l in tfs) / N
    phrase = query.lower().strip()
    scored = []
    for i, (tf, ln) in enumerate(tfs):
        s = 0.0
        for w in q_terms:
            f = tf.get(w, 0)
            if not f:
                continue
            idf = math.log(1 + (N - df[w] + 0.5) / (df[w] + 0.5))
            s += idf * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * ln / avg))
        if s > 0 and len(q_terms) > 1 and phrase in docs[i][2].lower():
            s *= 1.5
        if s > 0:
            scored.append((s, i))
    scored.sort(reverse=True)
    results = []
    from .citations import build_quote_citation
    from .pipeline import crop_url, viewer_url

    new = {}
    meta = dict(doc.meta, sha256=doc.sha)
    for s, i in scored[:limit]:
        p, para, text = docs[i]
        info = pm["pages"][p - 1]
        c = build_quote_citation(meta, info, p, para, None, "search")
        c["crop_url"] = crop_url(doc.sha, c["citation_id"])
        c["viewer_url"] = viewer_url(doc.sha, c["citation_id"])
        new[c["citation_id"]] = c
        results.append({"score": round(s, 3), "pdf_page": p, "printed_page": info.get("printed_page"), "page_kind": info.get("kind"), "text": text, "citation_id": c["citation_id"], "provenance": "quoted"})
    doc._store_citations(new)
    timings["rank"] = _ms(t1)
    return {"doc_id": doc.sha, "query": query, "results": results, "searched_pages": len(pages), "timings_ms": timings, "cache_hit": hit}


def note_summary(idx: list[dict]) -> list[dict[str, Any]]:
    return [{"note": e["note"], "title": e["title"], "pdf_page": e["start_page"], "end_pdf_page": e["end_page"]} for e in idx]
