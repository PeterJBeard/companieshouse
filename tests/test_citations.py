"""Pipeline, citation and trust tests (brief section 15.12) on a synthetic text-layer filing."""

from __future__ import annotations

import io

from PIL import Image

from ch_accounts_mcp.citations import check_answer, extract_claims, has_required_spans, span_matches_tokens
from ch_accounts_mcp.compute import compute


def _figure_citations(doc):
    return [c for c in doc.citations().values() if c.get("type") == "figure"]


def test_page_map_finds_statements(synthetic_doc):
    pm, _, _ = synthetic_doc.page_map()
    kinds = [p["kind"] for p in pm["pages"]]
    assert kinds[1] == "income_statement" and kinds[2] == "balance_sheet"
    assert pm["scanned_pages"] == []


def test_every_figure_verified_and_on_a_page(synthetic_doc):
    res, _, _ = synthetic_doc.primary_statements()
    s = res["verification"]["summary"]
    assert s["figures"] > 40 and s["unverified"] == 0
    for c in _figure_citations(synthetic_doc):
        assert c["pdf_page"] >= 1  # acceptance criterion 4


def test_trust_1_verbatim_spans_match_stored_tokens(synthetic_doc):
    for c in _figure_citations(synthetic_doc):
        toks = synthetic_doc._cached_tokens(c["pdf_page"])
        for span in c["spans"]:
            assert span_matches_tokens(span, toks), (c["citation_id"], span)


def test_trust_2_figure_citations_carry_label_value_header_unit(synthetic_doc):
    cits = _figure_citations(synthetic_doc)
    labelled = [c for c in cits if c["label"]]
    assert labelled and all(has_required_spans(c) for c in labelled)
    c = next(c for c in cits if c["label"] == "Debtors" and c["column"] == "2024")
    assert c["unit"] == "£'000" and c["period"] == "current" and c["note"] == "13"
    assert c["status"] == "extracted_verified" and c["checks"]


def test_citation_ids_are_deterministic(tmp_settings):
    from ch_accounts_mcp import pipeline
    from ch_accounts_mcp.cache import reset_cache

    from .conftest import make_pdf

    data = make_pdf()
    d1 = pipeline.Document.from_bytes(data, {"source": "test"})
    d1.primary_statements()
    ids1 = set(d1.citations())
    reset_cache(tmp_settings.cache_dir / "second")
    pipeline._open_docs.clear()
    d2 = pipeline.Document.from_bytes(data, {"source": "test"})
    d2.primary_statements()
    assert ids1 == set(d2.citations())


def test_crop_is_png_of_the_row(synthetic_doc):
    c = next(c for c in _figure_citations(synthetic_doc) if c["label"] == "Net assets")
    png = synthetic_doc.crop(c["citation_id"])
    img = Image.open(io.BytesIO(png))
    assert img.format == "PNG" and img.width > 300 and img.height > 30


def test_trust_5_check_answer_catches_planted_numbers(synthetic_doc):
    draft = (
        "Turnover was £12,000k in 2024 (p.2), up from 11,000. Net assets were £5.0m. "
        "Debtors stood at 1,500. Exceptional costs were £3,456k and cash rose to £9,999k. "
        "The gross margin was 47.5%."
    )
    res = check_answer(synthetic_doc, draft, search_all_pages=False)
    by_text = {i["text"]: i for i in res["items"]}
    assert by_text["£12,000k"]["status"] == "matched"
    assert by_text["11,000"]["status"] == "matched"
    assert by_text["£5.0m"]["status"] == "matched"
    assert by_text["1,500"]["status"] == "matched"
    assert by_text["£3,456k"]["status"] == "unmatched"
    assert by_text["£9,999k"]["status"] == "unmatched"
    assert by_text["47.5%"]["status"] == "unmatched"  # no computed margin yet: percentages need compute
    assert "⟦✖ UNMATCHED⟧" in res["tagged_draft"]


def test_check_answer_accepts_computed_percentages(synthetic_doc):
    gm = compute(synthetic_doc, "gross_margin")
    assert gm["display"] == "33.3%"
    res = check_answer(synthetic_doc, "Gross margin was 33.3%.", search_all_pages=False)
    assert res["items"][0]["status"] == "matched" and res["items"][0]["citation_id"] == gm["citation_id"]


def test_claims_skip_years_and_page_refs():
    kinds = [c["text"] for c in extract_claims("In 2024 (see page 12 and note 13) revenue was £4.8m.")]
    assert kinds == ["£4.8m"]


def test_compute_cites_inputs(synthetic_doc):
    r = compute(synthetic_doc, "current_ratio")
    assert r["status"] == "computed" and r["inputs"] and all(i["citation_id"].startswith("c_") for i in r["inputs"])
    assert r["display"] == "1.43"  # (900 + 2,100) / 2,100
    r = compute(synthetic_doc, "operating_margin")
    assert r["display"] == "8.3%"
    cits = {c["label"]: c["citation_id"] for c in _figure_citations(synthetic_doc) if c["column"] == "2024"}
    r = compute(synthetic_doc, "custom", definition=f"{cits['Gross profit']} - {cits['Administrative expenses']}")
    assert r["value_exact"].startswith("7000") or r["display"] == "7,000" or r["value"] == 7000.0


def test_quote_must_be_verbatim(synthetic_doc):
    synthetic_doc.tokens([1], 200)
    assert synthetic_doc.quote("Annual report and financial statements") is not None
    assert synthetic_doc.quote("Annual review and financial statements") is None


def test_workpaper_export(synthetic_doc):
    from openpyxl import load_workbook

    from ch_accounts_mcp.workpaper import export_workpaper

    ids = [c["citation_id"] for c in _figure_citations(synthetic_doc)][:5]
    r = export_workpaper(synthetic_doc, ids, answer="Turnover was £12,000k and widgets were £1,234k.", reviewer="A Reviewer")
    wb = load_workbook(r["xlsx_path"])
    text = " ".join(str(c.value) for row in wb.active.iter_rows() for c in row if c.value)
    assert synthetic_doc.sha in text and "Reviewer sign-off" in text and "£1,234k" in text
    assert r["counts"]["unmatched_claims"] == 1
    import pymupdf

    pdf = pymupdf.open(r["pdf_path"])
    assert "Reviewer sign-off" in "".join(p.get_text() for p in pdf)
