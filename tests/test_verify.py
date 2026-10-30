from decimal import Decimal

from ch_accounts_mcp.layout import rebuild_page
from ch_accounts_mcp.verify import VERIFIED, cast_all, cast_rows, cross_check, figures_from_layout, is_subtotal_identity, summary

from .helpers import H, W, L, R, balance_sheet_lines, tokens


def _figs(lines, statement="balance_sheet", scope="company", page=1):
    lay = rebuild_page(tokens(lines), page, W, H)
    figs = figures_from_layout(lay, statement, scope, f"p{page}")
    return lay, figs


def _by(figs, label, key="2024"):
    return next(f for f in figs if f.label == label and f.key == key)


def test_clean_balance_sheet_all_verified():
    _, figs = _figs(balance_sheet_lines())
    cast_all(figs)
    cross_check(figs)
    s = summary(figs)
    assert s["unverified"] == 0 and s["figures"] == 30


def test_misread_value_is_unverified_and_its_column_neighbours_too():
    _, figs = _figs(balance_sheet_lines({"Debtors": "1,800"}))
    cast_all(figs)
    cross_check(figs)
    deb = _by(figs, "Debtors")
    assert deb.status == "unverified"
    assert _by(figs, "Stocks").status == "unverified"
    # the prior-year column is untouched
    assert _by(figs, "Debtors", "2023").status in VERIFIED


def test_casting_records_related_figures():
    _, figs = _figs(balance_sheet_lines())
    cast_all(figs)
    deb = _by(figs, "Debtors")
    rules = {c["rule"] for c in deb.checks}
    assert "casts_to" in rules
    related = [r for c in deb.checks for r in c["related"]]
    assert related, "a cast figure must point to the total it feeds"


def test_zero_agreement_proves_nothing():
    lines = [
        (100, [R(440, "2024"), R(520, "2023")]),
        (130, [L(60, "Exceptional items"), R(440, "-"), R(520, "100")]),
        (145, [L(60, "Other"), R(440, "-"), R(520, "50")]),
    ]
    _, figs = _figs(lines, "income_statement")
    cast_all(figs)
    assert all(f.status == "unverified" for f in figs if f.key == "2024")


def test_rounding_match_is_only_provisional():
    lines = [
        (100, [R(440, "2024"), R(520, "2023")]),
        (112, [R(440, "£m"), R(520, "£m")]),
        (130, [L(60, "Revenue"), R(440, "10.4"), R(520, "9.0")]),
        (145, [L(60, "Other income"), R(440, "2.4"), R(520, "1.0")]),
        (160, [L(60, "Total income"), R(440, "12.9"), R(520, "10.0")]),
    ]
    _, figs = _figs(lines, "income_statement")
    cast_all(figs)
    tot = _by(figs, "Total income")
    assert tot.status == "unverified" and tot.provisional_rounding
    assert _by(figs, "Total income", "2023").status == "verified"


def test_net_assets_agree_to_equity_cross_check():
    _, figs = _figs(balance_sheet_lines())
    cast_all(figs)
    res = cross_check(figs)
    assert any(r["rule"] == "net_assets_equals_equity" for r in res)


def test_ifrs_subtotal_identity_after_total_assets():
    # IFRS order: total assets sits between the current-assets subtotal and net current liabilities
    lines = [
        (100, [R(440, "2024"), R(520, "2023")]),
        (112, [R(440, "£m"), R(520, "£m")]),
        (130, [L(60, "Intangible assets"), R(440, "666"), R(520, "600")]),
        (145, [L(60, "Property, plant and equipment"), R(440, "495"), R(520, "500")]),
        (160, [R(440, "1,161"), R(520, "1,100")]),
        (175, [L(60, "Inventories"), R(440, "699"), R(520, "705")]),
        (190, [L(60, "Cash and bank balances"), R(440, "55"), R(520, "30")]),
        (205, [R(440, "754"), R(520, "735")]),
        (220, [L(60, "Total assets"), R(440, "1,915"), R(520, "1,835")]),
        (235, [L(60, "Trade and other payables"), R(440, "(629)"), R(520, "(666)")]),
        (250, [L(60, "Provisions"), R(440, "(6)"), R(520, "(20)")]),
        (265, [R(440, "(635)"), R(520, "(686)")]),
        (280, [L(60, "Net current llabltes"), R(440, "119"), R(520, "49")]),
    ]
    _, figs = _figs(lines)
    cast_all(figs)
    ncl = _by(figs, "Net current llabltes")
    assert ncl.status == "verified"
    assert any(c["rule"] == "casts_subtotals" for c in ncl.checks)


def test_subtotal_identity_labels_are_fuzzy_but_narrow():
    assert is_subtotal_identity("Net current assets")
    assert is_subtotal_identity("Totalliabllities")
    assert not is_subtotal_identity("Total assets")
    assert not is_subtotal_identity("Net cash from operating activities")


def test_equity_statement_row_casting():
    lines = [
        (100, [R(300, "Share"), R(380, "Retained"), R(460, "Total")]),
        (112, [R(300, "capital"), R(380, "earnings"), R(460, "equity")]),
        (124, [R(300, "£'000"), R(380, "£'000"), R(460, "£'000")]),
        (140, [L(60, "At 1 January 2024"), R(300, "100"), R(380, "4,400"), R(460, "4,500")]),
        (155, [L(60, "Profit for the year"), R(300, "-"), R(380, "700"), R(460, "700")]),
        (170, [L(60, "Dividends paid"), R(300, "-"), R(380, "(200)"), R(460, "(200)")]),
        (185, [L(60, "At 31 December 2024"), R(300, "100"), R(380, "4,900"), R(460, "5,000")]),
    ]
    lay, figs = _figs(lines, "equity_statement")
    cast_all(figs)
    cast_rows(figs, {(1, i): s for i, s in enumerate(lay.sections)})
    opening = [f for f in figs if f.label == "At 1 January 2024"]
    assert opening and all(f.status in VERIFIED for f in opening)
    closing_total = next(f for f in figs if f.label == "At 31 December 2024" and "total" in f.key)
    assert closing_total.value == Decimal(5000) and closing_total.status in VERIFIED


def test_balance_sheet_equity_agrees_to_equity_statement_closing_row():
    _, bs = _figs(balance_sheet_lines(), page=1)
    lines = [
        (100, [R(300, "Share"), R(380, "Profit and"), R(460, "Total")]),
        (112, [R(300, "capital"), R(380, "loss account"), R(460, "equity")]),
        (124, [R(300, "£'000"), R(380, "£'000"), R(460, "£'000")]),
        (140, [L(60, "At 1 January 2024"), R(300, "100"), R(380, "4,400"), R(460, "4,500")]),
        (155, [L(60, "Profit for the year"), R(300, "-"), R(380, "500"), R(460, "500")]),
        (170, [L(60, "At 31 December 2024"), R(300, "100"), R(380, "4,900"), R(460, "5,000")]),
    ]
    _, soce = _figs(lines, "equity_statement", page=2)
    figs = bs + soce
    cast_all(figs)
    cross_check(figs)
    pl = _by(bs, "Profit and loss account")
    assert any(c["rule"] == "agrees_equity_statement" for c in pl.checks)


def test_two_equal_detail_lines_do_not_verify_each_other():
    lines = [
        (100, [R(440, "2024"), R(520, "2023")]),
        (130, [L(60, "Deferred tax assets"), R(440, "119"), R(520, "1")]),
        (145, [L(60, "Trade and other receivables"), R(440, "11"), R(520, "1")]),
    ]
    _, figs = _figs(lines)
    cast_all(figs)
    assert all(f.status == "unverified" for f in figs)


def test_single_line_block_subtotal_still_agrees():
    lines = [
        (100, [R(440, "2024"), R(520, "2023")]),
        (130, [L(60, "Deferred tax liabilities"), R(440, "(69)"), R(520, "(50)")]),
        (145, [R(440, "(69)"), R(520, "(50)")]),
    ]
    _, figs = _figs(lines)
    cast_all(figs)
    assert all(f.status in VERIFIED for f in figs)
