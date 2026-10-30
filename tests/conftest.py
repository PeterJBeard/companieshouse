"""Fixtures: a synthetic text-layer filing run through the full pipeline (no OCR needed)."""

from __future__ import annotations

import dataclasses

import pymupdf
import pytest

from ch_accounts_mcp import pipeline
from ch_accounts_mcp.cache import reset_cache
from ch_accounts_mcp.config import override_settings, settings

from .helpers import L, R, balance_sheet_lines

FONT = 9.0


def _draw(page: pymupdf.Page, lines) -> None:
    for y, items in lines:
        for align, x, text in items:
            w = pymupdf.get_text_length(text, fontname="helv", fontsize=FONT)
            x0 = x if align == "L" else x - w
            page.insert_text((x0, y + FONT), text, fontname="helv", fontsize=FONT)


def income_lines():
    return [
        (120, [L(60, "Profit and loss account for the year ended 31 December 2024")]),
        (150, [L(330, "Note"), R(440, "2024"), R(520, "2023")]),
        (162, [R(440, "£'000"), R(520, "£'000")]),
        (180, [L(60, "Turnover"), L(335, "3"), R(440, "12,000"), R(520, "11,000")]),
        (195, [L(60, "Cost of sales"), R(440, "(8,000)"), R(520, "(7,500)")]),
        (210, [L(60, "Gross profit"), R(440, "4,000"), R(520, "3,500")]),
        (225, [L(60, "Administrative expenses"), R(440, "(3,000)"), R(520, "(2,700)")]),
        (240, [L(60, "Operating profit"), L(335, "4"), R(440, "1,000"), R(520, "800")]),
        (255, [L(60, "Interest payable and similar expenses"), R(440, "(100)"), R(520, "(90)")]),
        (270, [L(60, "Profit before taxation"), R(440, "900"), R(520, "710")]),
        (285, [L(60, "Tax on profit"), L(335, "5"), R(440, "(200)"), R(520, "(150)")]),
        (300, [L(60, "Profit for the financial year"), R(440, "700"), R(520, "560")]),
    ]


def make_pdf() -> bytes:
    doc = pymupdf.open()
    p1 = doc.new_page(width=595, height=842)
    _draw(
        p1,
        [
            (200, [L(150, "Example Widgets Limited")]),
            (220, [L(150, "Annual report and financial statements")]),
            (240, [L(150, "for the year ended 31 December 2024")]),
            (300, [L(150, "Registered number 01234567")]),
            (320, [L(150, "These pages are a synthetic test fixture made for the unit tests only")]),
        ],
    )
    _draw(doc.new_page(width=595, height=842), income_lines())
    _draw(doc.new_page(width=595, height=842), balance_sheet_lines())
    data = doc.tobytes()
    doc.close()
    return data


@pytest.fixture()
def tmp_settings(tmp_path, monkeypatch):
    old = settings()
    s = dataclasses.replace(old, cache_dir=tmp_path / "cache", public_base_url="http://testserver", mcp_bearer_token=None, companies_house_api_key=None)
    override_settings(s)
    reset_cache(s.cache_dir)
    pipeline._open_docs.clear()
    yield s
    override_settings(old)
    reset_cache(old.cache_dir)
    pipeline._open_docs.clear()


@pytest.fixture()
def synthetic_doc(tmp_settings):
    from ch_accounts_mcp.companies_house import now_iso

    doc = pipeline.Document.from_bytes(make_pdf(), {"source": "test", "retrieved_at": now_iso(), "company_number": "01234567"})
    doc.primary_statements()
    return doc
