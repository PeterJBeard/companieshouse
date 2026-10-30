from decimal import Decimal

import pytest

from ch_accounts_mcp.layout import canonical_unit, is_unit, parse_number, rebuild_page, to_markdown
from ch_accounts_mcp.triage import build_notes_index, classify_line, is_running_header, note_heading_candidates, running_header_page_number

from .helpers import H, W, L, R, balance_sheet_lines, tokens


# --------------------------------------------------------------------------- number parsing


@pytest.mark.parametrize(
    "raw,value,decimals",
    [
        ("1,234", Decimal(1234), 0),
        ("(1,234)", Decimal(-1234), 0),
        ("-1,234", Decimal(-1234), 0),
        ("1,234.5", Decimal("1234.5"), 1),
        ("(0.6)", Decimal("-0.6"), 1),
        ("£4,812", Decimal(4812), 0),
        ("12", Decimal(12), 0),
    ],
)
def test_parse_number(raw, value, decimals):
    n = parse_number(raw)
    assert n is not None and n.value == value and n.decimals == decimals and not n.low_confidence


@pytest.mark.parametrize("raw", ["-", "–", "—", "(-)"])
def test_dashes_are_zero(raw):
    n = parse_number(raw)
    assert n is not None and n.is_dash and n.value == 0


def test_confusable_characters_are_flagged():
    n = parse_number("1,2O4")
    assert n is not None and n.value == 1204 and n.low_confidence


def test_thousands_read_as_full_stops():
    n = parse_number("1.168.3")
    assert n is not None and n.value == Decimal("1168.3") and n.low_confidence


@pytest.mark.parametrize("raw", ["£'000", "'000", "000", "£m", "£000", "US$'000", "f'000", "E'000"])
def test_units_are_not_numbers(raw):
    assert is_unit(raw)
    assert parse_number(raw) is None or raw in ("£m",)


def test_canonical_unit():
    assert canonical_unit("000") == "£'000"
    assert canonical_unit("f'000") == "£'000"
    assert canonical_unit("£000") == "£'000"
    assert canonical_unit("£m") == "£m"


def test_text_is_not_a_number():
    assert parse_number("Debtors") is None
    assert parse_number("note") is None


# --------------------------------------------------------------------------- layout rebuild


def _layout(lines):
    return rebuild_page(tokens(lines), 1, W, H)


def test_balance_sheet_columns_units_and_notes():
    lay = _layout(balance_sheet_lines())
    assert lay.unit == "£'000"
    s = lay.sections[0]
    vals = [c for c in s.columns if not c.is_note]
    assert [c.year for c in vals] == [2024, 2023]
    assert [c.period for c in vals] == ["current", "prior"]
    assert any(c.is_note for c in s.columns)
    rows = {r.label: r for r in s.rows if r.label}
    assert rows["Debtors"].note == "13"
    assert rows["Debtors"].values["2024"].num.value == 1500
    assert rows["Creditors: amounts falling due within one year"].values["2023"].num.value == -2000
    assert "Fixed assets" in rows and not rows["Fixed assets"].values


def test_unlabelled_subtotals_are_kept():
    lay = _layout(balance_sheet_lines())
    subtotals = [r for r in lay.sections[0].rows if not r.label and r.values]
    assert [r.values["2024"].num.value for r in subtotals] == [4600, 3000]


def test_markdown_marks_unverified_values():
    lay = _layout(balance_sheet_lines())
    debtors = next(r for r in lay.sections[0].rows if r.label == "Debtors")
    md = to_markdown(lay, {id(debtors.values["2024"]): "unverified"})
    assert "| Debtors | 13 | 1,500 (?) | 1,400 |" in md


def test_wrapped_label_is_joined():
    lines = [
        (100, [R(440, "2024"), R(520, "2023")]),
        (112, [R(440, "£'000"), R(520, "£'000")]),
        (130, [L(60, "Amounts owed by group undertakings and")]),
        (141, [L(70, "related parties"), R(440, "1,000"), R(520, "900")]),
        (156, [L(60, "Other debtors"), R(440, "200"), R(520, "100")]),
        (171, [R(440, "1,200"), R(520, "1,000")]),
    ]
    rows = [r for r in _layout(lines).sections[0].rows if r.values and r.label]
    assert rows[0].label == "Amounts owed by group undertakings and related parties"


def test_group_and_company_columns():
    lines = [
        (100, [R(330, "Group"), R(400, "Group"), R(470, "Company"), R(540, "Company")]),
        (112, [R(330, "2024"), R(400, "2023"), R(470, "2024"), R(540, "2023")]),
        (124, [R(330, "£m"), R(400, "£m"), R(470, "£m"), R(540, "£m")]),
        (140, [L(60, "Trade debtors"), R(330, "117.5"), R(400, "106.8"), R(470, "0.9"), R(540, "1.1")]),
        (155, [L(60, "Other debtors"), R(330, "98.4"), R(400, "103.2"), R(470, "61.3"), R(540, "73.5")]),
        (170, [R(330, "215.9"), R(400, "210.0"), R(470, "62.2"), R(540, "74.6")]),
    ]
    lay = _layout(lines)
    cols = [c for c in lay.sections[0].columns if not c.is_note]
    assert [(c.scope, c.year) for c in cols] == [("group", 2024), ("group", 2023), ("company", 2024), ("company", 2023)]
    assert lay.sections[0].unit == "£m" or all(c.unit == "£m" for c in cols)


def test_misread_thousands_separator_in_whole_number_column():
    lines = [
        (100, [R(440, "2024"), R(520, "2023")]),
        (112, [R(440, "£'000"), R(520, "£'000")]),
        (130, [L(60, "Revenue"), R(440, "2,518,138"), R(520, "2,373,827")]),
        (145, [L(60, "Cost of sales"), R(440, "(1,900,000)"), R(520, "(1,800,000)")]),
        (160, [L(60, "Tax on profit"), R(440, "(34,705)"), R(520, "(27.066)")]),
        (175, [L(60, "Other"), R(440, "1,000"), R(520, "2,000")]),
    ]
    rows = {r.label: r for r in _layout(lines).sections[0].rows if r.label}
    c = rows["Tax on profit"].values["2023"]
    assert c.num.value == -27066 and c.num.low_confidence
    assert c.text == "(27,066)" and c.ocr_text == "(27.066)"


def test_running_header_is_not_a_row():
    lines = [
        (20, [L(60, "Mott MacDonald Group Limited | Report and financial statements | 49")]),
        (100, [R(440, "2024"), R(520, "2023")]),
        (112, [R(440, "£'000"), R(520, "£'000")]),
        (130, [L(60, "Revenue"), R(440, "500"), R(520, "400")]),
    ]
    lay = _layout(lines)
    assert all("Report and financial statements" not in r.label for s in lay.sections for r in s.rows)


# --------------------------------------------------------------------------- triage


@pytest.mark.parametrize(
    "line,kind",
    [
        ("Consolidated balance sheet as at 31 December 2024", "balance_sheet"),
        ("Company statement of financial position", "balance_sheet"),
        ("Consolidated profit and loss account for the year ended 31 December 2024", "income_statement"),
        ("Consolidated statement of comprehensive income", "income_statement"),
        ("Consolidated statement of cash flows", "cash_flow"),
        ("Consolidated statement of changes in equity", "equity_statement"),
        ("Independent auditors' report to the members of X Limited", "audit_report"),
        ("Notes to the financial statements for the year ended 31 December 2024", "notes"),
        ("Strategic report for the year ended 31 December 2024", "strategic_report"),
        ("Directors' report for the year ended 31 December 2024", "directors_report"),
        ("Statement of directors' responsibilities", "responsibilities"),
    ],
)
def test_classify_line(line, kind):
    got = classify_line(line)
    assert got is not None and got[0] == kind


def test_cover_title_is_front_not_directors_report():
    got = classify_line("Directors' Report and Consolidated Financial Statements")
    assert got is not None and got[0] == "front"


def test_running_header_detection():
    assert is_running_header("Tesco Stores Limited | Annual report and financial statements 2025 | 12")
    assert running_header_page_number("Mott MacDonald Group Limited | Report and financial statements | 49") == "49"


def test_notes_index_keeps_increasing_chain():
    lines = [
        (60, [L(60, "12. Debtors")]),
        (200, [L(60, "13. Creditors")]),
        (300, [L(70, "1. this is a list item inside a note")]),
        (400, [L(60, "14. Provisions for liabilities")]),
    ]
    cands = note_heading_candidates(5, tokens(lines), W)
    idx = build_notes_index(cands)
    assert [n["note"] for n in idx] == [12, 13, 14]
    assert idx[0]["title"] == "Debtors"
