"""Accuracy and latency harness over the hand-keyed gold set (brief section 12).

    python bench.py                  # all gold filings, 1 cold + 3 warm runs each
    python bench.py --repeats 2 --json bench_results.json
    python bench.py --only jcb_2024

For each filing the PDF is fetched (and SHA-256 checked) by tests/gold/fetch.py, then run
through the real MCP tools in-process (load_accounts, get_primary_statements):

* cold  - a fresh empty CACHE_DIR and no open documents; the OCR worker pool is already
          started (its one-off start-up is timed separately), as in a long-running server.
* warm  - the same process again: disk cache and in-memory document both warm.

Gold rows are aligned to the extracted rows on the same PDF page by order and fuzzy label
(empty labels pair with empty labels), and values are compared in the unit as filed.

  precision        verified figures that equal gold / all verified figures on gold pages
                   (verified figures with no gold counterpart count against precision)
  recall           gold figures returned verified and equal to gold / all gold figures
  accuracy (any)   gold figures returned with the right value, whatever the status
  share unverified unverified figures / all figures extracted on gold pages
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import platform
import re
import statistics
import sys
import tempfile
import time
from difflib import SequenceMatcher
from pathlib import Path


def _hardware() -> str:
    cpu = platform.processor() or "unknown CPU"
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    mem = ""
    try:
        kb = int(next(l for l in Path("/proc/meminfo").read_text().splitlines() if l.startswith("MemTotal")).split()[1])
        mem = f", {kb / 1024 / 1024:.0f} GB RAM"
    except (OSError, StopIteration, ValueError):
        pass
    return f"{os.cpu_count()} vCPU {cpu}{mem}, {platform.system()} {platform.release()}, Python {platform.python_version()}"


def _norm(label: str) -> str:
    return re.sub(r"[^a-z]+", " ", (label or "").lower()).strip()


def _sim(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def align(gold_labels: list[str], ext_labels: list[str], threshold: float = 0.6) -> list[tuple[int, int]]:
    """Order-preserving alignment maximising total label similarity (Needleman-Wunsch, zero gap cost)."""
    n, m = len(gold_labels), len(ext_labels)
    score = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            s = _sim(gold_labels[i], ext_labels[j])
            best = max(score[i + 1][j], score[i][j + 1])
            if s >= threshold:
                best = max(best, s + score[i + 1][j + 1])
            score[i][j] = best
    pairs, i, j = [], 0, 0
    while i < n and j < m:
        s = _sim(gold_labels[i], ext_labels[j])
        if s >= threshold and abs(score[i][j] - (s + score[i + 1][j + 1])) < 1e-9:
            pairs.append((i, j))
            i, j = i + 1, j + 1
        elif score[i][j] == score[i + 1][j]:
            i += 1
        else:
            j += 1
    return pairs


def _page_rows(result: dict, page: int, scope: str) -> list[tuple[dict, list[str]]]:
    """Extracted value rows on one page, each with its column keys ordered current year first."""
    out = []
    for st in result["statements"]:
        if page not in st["pages"] or st["scope"] not in (scope, "unknown"):
            continue
        cols = sorted(st["columns"], key=lambda c: (c["year"] is None, -(c["year"] or 0)))
        keys = [c["key"] for c in cols]
        for r in st["rows"]:
            if r.get("page") == page and r.get("values"):
                out.append((r, keys))
    out.sort(key=lambda t: t[0]["y"])
    return out


def score_filing(gold: dict, result: dict) -> dict:
    s = {"gold_figures": 0, "verified_correct": 0, "verified_wrong": 0, "unverified_correct": 0, "unverified_wrong": 0, "missing": 0, "extra_verified": 0, "extra_unverified": 0, "extracted_on_gold_pages": 0, "unverified_on_gold_pages": 0}
    errors = []
    for gp in gold["pages"]:
        ext = _page_rows(result, gp["pdf_page"], gp["scope"])
        used: set[tuple[int, str]] = set()
        for r, _ in ext:
            for v in r["values"].values():
                s["extracted_on_gold_pages"] += 1
                s["unverified_on_gold_pages"] += v["status"] == "unverified"
        pairs = dict(align([g[0] for g in gp["rows"]], [r["label"] for r, _ in ext]))
        for gi, grow in enumerate(gp["rows"]):
            for yi, want in enumerate(grow[1:]):
                if want is None:
                    continue
                s["gold_figures"] += 1
                year = str(gp["years"][yi])
                got = None
                if gi in pairs:
                    r, keys = ext[pairs[gi]]
                    key = year if year in r["values"] else (keys[yi] if yi < len(keys) else None)
                    if key in r["values"]:
                        got = r["values"][key]
                        used.add((pairs[gi], key))
                if got is None:
                    s["missing"] += 1
                    errors.append({"page": gp["pdf_page"], "label": grow[0], "year": year, "gold": want, "got": None})
                    continue
                ok = abs(got["value"] - want) < 1e-6
                verified = got["status"] != "unverified"
                s[("verified_" if verified else "unverified_") + ("correct" if ok else "wrong")] += 1
                if not ok or not verified:
                    errors.append({"page": gp["pdf_page"], "label": grow[0], "year": year, "gold": want, "got": got["value"], "raw": got["raw"], "status": got["status"], "citation_id": got["citation_id"]})
        for j, (r, _) in enumerate(ext):
            for k, v in r["values"].items():
                if (j, k) not in used:
                    s["extra_" + ("unverified" if v["status"] == "unverified" else "verified")] += 1
    verified_total = s["verified_correct"] + s["verified_wrong"] + s["extra_verified"]
    s["precision"] = s["verified_correct"] / verified_total if verified_total else None
    s["recall"] = s["verified_correct"] / s["gold_figures"] if s["gold_figures"] else None
    s["accuracy_any_status"] = (s["verified_correct"] + s["unverified_correct"]) / s["gold_figures"] if s["gold_figures"] else None
    s["share_unverified"] = s["unverified_on_gold_pages"] / s["extracted_on_gold_pages"] if s["extracted_on_gold_pages"] else None
    s["errors"] = errors
    return s


def trust_checks(doc) -> dict:
    """Brief 15.12 (1) and (2) over every figure citation of a real filing."""
    from ch_accounts_mcp import ocr
    from ch_accounts_mcp.citations import has_required_spans, span_matches_tokens

    cits = [c for c in doc.citations().values() if c.get("type") == "figure"]
    tok_sets: dict[int, list[list[dict]]] = {}
    for c in cits:
        pg = c["pdf_page"]
        if pg not in tok_sets:
            if doc.is_text_page(pg):
                tok_sets[pg] = [doc.cache.get_json(doc.sha, f"pages/{pg}.tokens.text.json") or []]
            else:
                tok_sets[pg] = [x["tokens"] for d in (ocr.FULL_DPI, ocr.REREAD_DPI) if (x := doc.cache.get_json(doc.sha, f"pages/{pg}.tokens.{d}.json"))]
    spans = bad_spans = 0
    for c in cits:
        for s in c["spans"]:
            spans += 1
            bad_spans += not any(span_matches_tokens(s, toks) for toks in tok_sets[c["pdf_page"]])
    labelled = [c for c in cits if c["label"]]
    missing = sum(not has_required_spans(c) for c in labelled)
    return {"figure_citations": len(cits), "spans": spans, "spans_not_matching_tokens": bad_spans, "labelled_citations": len(labelled), "citations_missing_required_spans": missing}


def _call(server, name: str, args: dict) -> dict:
    res = asyncio.run(server.mcp.call_tool(name, args))
    content = res[0] if isinstance(res, tuple) else res
    body = json.loads(next(c.text for c in content if c.type == "text"))
    if "error" in body:
        raise RuntimeError(f"{name}: {body['error']}")
    return body


def _pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def _fmt(x: float | None) -> str:
    return "  n/a " if x is None else f"{100 * x:6.2f}%"


def main() -> None:
    ap = argparse.ArgumentParser(description="accuracy and latency bench over tests/gold")
    ap.add_argument("--repeats", type=int, default=1, help="cold runs per filing (each OCRs the filing from scratch)")
    ap.add_argument("--warm", type=int, default=3, help="warm runs per filing")
    ap.add_argument("--only", action="append", help="gold file stem(s) to run")
    ap.add_argument("--pdf-dir", type=Path, default=None)
    ap.add_argument("--json", type=Path, default=None, help="write full results (including every mismatch) here")
    args = ap.parse_args()

    os.environ.setdefault("START_VIEWER", "0")
    from ch_accounts_mcp import ocr, pipeline, server
    from ch_accounts_mcp.cache import reset_cache
    from ch_accounts_mcp.config import override_settings, settings
    from tests.gold.fetch import DEFAULT_PDF_DIR, ensure_pdf, gold_files, load_gold

    base = settings()
    golds = [(g.stem, load_gold(g)) for g in gold_files() if not args.only or g.stem in args.only]
    pdfs = {stem: ensure_pdf(g, args.pdf_dir or DEFAULT_PDF_DIR) for stem, g in golds}

    t0 = time.perf_counter()
    ocr.get_pool().warm()
    pool_ms = (time.perf_counter() - t0) * 1000

    print(f"hardware: {_hardware()}")
    print(f"ocr: engine={base.ocr_engine} device={base.ocr_device} workers={base.ocr_workers}; pool start-up {pool_ms:.0f} ms (once per server process, excluded below)")
    print(f"gold: {len(golds)} filings, {sum(len(r) for _, g in golds for p in g['pages'] for r in [p['rows']])} rows\n")

    lat: dict[str, list[float]] = {"load_cold": [], "primary_cold": [], "end_to_end_cold": [], "load_warm": [], "primary_warm": []}
    per: dict[str, dict] = {}
    with tempfile.TemporaryDirectory(prefix="ch_bench_") as tmp:
        for stem, gold in golds:
            scores = None
            for rep in range(args.repeats):
                cache_dir = Path(tmp) / f"{stem}_{rep}"
                override_settings(dataclasses.replace(base, cache_dir=cache_dir))
                reset_cache(cache_dir)
                pipeline._open_docs.clear()
                load_args = {"pdf_path": str(pdfs[stem]), "company_number": gold["company_number"]}
                t = time.perf_counter()
                loaded = _call(server, "load_accounts", load_args)
                t_load = (time.perf_counter() - t) * 1000
                t = time.perf_counter()
                prim = _call(server, "get_primary_statements", {"doc_id": loaded["doc_id"]})
                t_prim = (time.perf_counter() - t) * 1000
                assert not loaded["cache_hit"] and not prim["cache_hit"], "cold run hit the cache"
                lat["load_cold"].append(t_load)
                lat["primary_cold"].append(t_prim)
                lat["end_to_end_cold"].append(t_load + t_prim)
                if scores is None:
                    result = pipeline.Document.open(loaded["doc_id"]).primary_statements()[0]
                    scores = score_filing(gold, result)
                    scores["trust"] = trust_checks(pipeline.Document.open(loaded["doc_id"]))
                    scores.update(pages=loaded["page_count"], document_type=loaded["document_type"], doc_summary=prim["verification"]["summary"])
            for _ in range(args.warm):
                t = time.perf_counter()
                loaded = _call(server, "load_accounts", load_args)
                lat["load_warm"].append((time.perf_counter() - t) * 1000)
                t = time.perf_counter()
                _call(server, "get_primary_statements", {"doc_id": loaded["doc_id"]})
                lat["primary_warm"].append((time.perf_counter() - t) * 1000)
            scores["cold_ms"] = {"load_accounts": round(t_load), "get_primary_statements": round(t_prim)}
            per[stem] = scores
            print(f"{stem:24s} {scores['pages']:3d}pp {scores['document_type']:8s} gold={scores['gold_figures']:3d}  precision={_fmt(scores['precision'])}  recall={_fmt(scores['recall'])}  "
                  f"any-status={_fmt(scores['accuracy_any_status'])}  unverified={_fmt(scores['share_unverified'])}  verified-wrong={scores['verified_wrong']}  "
                  f"cold load {t_load / 1000:.1f}s + primary {t_prim / 1000:.1f}s")
            tr = scores["trust"]
            print(f"{'':24s} trust 15.12(1): {tr['spans'] - tr['spans_not_matching_tokens']}/{tr['spans']} spans equal the OCR tokens in their box; "
                  f"15.12(2): {tr['labelled_citations'] - tr['citations_missing_required_spans']}/{tr['labelled_citations']} labelled figure citations carry label, value, header and unit")
    override_settings(base)
    reset_cache(base.cache_dir)

    tot = {k: sum(p[k] for p in per.values()) for k in ("gold_figures", "verified_correct", "verified_wrong", "unverified_correct", "unverified_wrong", "missing", "extra_verified", "extra_unverified", "extracted_on_gold_pages", "unverified_on_gold_pages")}
    vt = tot["verified_correct"] + tot["verified_wrong"] + tot["extra_verified"]
    summary = {
        "precision": tot["verified_correct"] / vt if vt else None,
        "recall": tot["verified_correct"] / tot["gold_figures"],
        "accuracy_any_status": (tot["verified_correct"] + tot["unverified_correct"]) / tot["gold_figures"],
        "share_unverified": tot["unverified_on_gold_pages"] / tot["extracted_on_gold_pages"],
        **tot,
    }
    print("\nall filings")
    print(f"  gold figures {tot['gold_figures']}; returned verified and correct {tot['verified_correct']}; verified but WRONG {tot['verified_wrong']}; "
          f"unverified {tot['unverified_correct'] + tot['unverified_wrong']} ({tot['unverified_wrong']} of them wrong); missing {tot['missing']}; "
          f"extra figures with no gold row {tot['extra_verified']} verified / {tot['extra_unverified']} unverified")
    print(f"  precision {_fmt(summary['precision'])}   recall {_fmt(summary['recall'])}   accuracy (any status) {_fmt(summary['accuracy_any_status'])}   share unverified {_fmt(summary['share_unverified'])}")
    print("\nlatency (ms)                      n     p50     p95     max")
    lat_out = {}
    for k, xs in lat.items():
        if xs:
            lat_out[k] = {"n": len(xs), "p50": round(_pct(xs, 0.5)), "p95": round(_pct(xs, 0.95)), "max": round(max(xs))}
            print(f"  {k:28s} {len(xs):4d} {lat_out[k]['p50']:7d} {lat_out[k]['p95']:7d} {lat_out[k]['max']:7d}")
    if any(p["errors"] for p in per.values()):
        print("\nmismatches and unverified gold figures")
        for stem, p in per.items():
            for e in p["errors"]:
                print(f"  {stem}: p{e['page']} {e['label'] or '(unlabelled subtotal)'} [{e['year']}] gold={e['gold']} got={e['got']} {e.get('raw', '')} {e.get('status', 'missing')}")
    if args.json:
        args.json.write_text(json.dumps({"hardware": _hardware(), "ocr_engine": base.ocr_engine, "ocr_device": base.ocr_device, "ocr_workers": base.ocr_workers, "pool_startup_ms": round(pool_ms), "summary": summary, "latency_ms": lat_out, "filings": per}, indent=2))
    ocr.get_pool().shutdown()
    sys.exit(1 if tot["verified_wrong"] else 0)


if __name__ == "__main__":
    main()
