"""Hand-made OCR tokens for layout and verification tests.

Tokens mimic what the OCR layer produces: {"b": [x0, y0, x1, y1] in PDF points, "t": text, "c": conf, "l": line}.
A 10 pt font is approximated as 5 pt per character.
"""

from __future__ import annotations

CHAR_W = 5.0
LINE_H = 9.0

W, H = 595.0, 842.0


def L(x0: float, text: str) -> tuple[str, float, str]:
    """Left-aligned text starting at x0."""
    return ("L", x0, text)


def R(x1: float, text: str) -> tuple[str, float, str]:
    """Right-aligned text ending at x1 (numbers in value columns)."""
    return ("R", x1, text)


def tokens(lines: list[tuple[float, list[tuple[str, float, str]]]]) -> list[dict]:
    out = []
    for li, (y, items) in enumerate(lines):
        for align, x, text in items:
            words = text.split(" ")
            total = len(text) * CHAR_W
            x0 = x if align == "L" else x - total
            for w in words:
                w_len = len(w) * CHAR_W
                out.append({"b": [round(x0, 2), y, round(x0 + w_len, 2), y + LINE_H], "t": w, "c": 0.99, "l": li})
                x0 += w_len + CHAR_W
    return out


def balance_sheet_lines(values_2024: dict[str, str] | None = None) -> list:
    """A small FRS 102 style balance sheet with two year columns in £'000."""
    v = {
        "Intangible assets": ("1,200", "1,000"),
        "Tangible assets": ("3,400", "3,100"),
        "fixed_total": ("4,600", "4,100"),
        "Stocks": ("800", "750"),
        "Debtors": ("1,500", "1,400"),
        "Cash at bank and in hand": ("700", "650"),
        "current_total": ("3,000", "2,800"),
        "Creditors: amounts falling due within one year": ("(2,100)", "(2,000)"),
        "Net current assets": ("900", "800"),
        "Total assets less current liabilities": ("5,500", "4,900"),
        "Provisions for liabilities": ("(500)", "(400)"),
        "Net assets": ("5,000", "4,500"),
        "Called up share capital": ("100", "100"),
        "Profit and loss account": ("4,900", "4,400"),
        "Total shareholders' funds": ("5,000", "4,500"),
    }
    if values_2024:
        for k, x in values_2024.items():
            v[k] = (x, v[k][1])
    y = 120.0
    lines = [
        (y, [L(60, "Balance sheet as at 31 December 2024")]),
        (y + 30, [L(330, "Note"), R(440, "2024"), R(520, "2023")]),
        (y + 42, [R(440, "£'000"), R(520, "£'000")]),
        (y + 60, [L(60, "Fixed assets")]),
    ]
    yy = y + 75
    order = [
        ("Intangible assets", "10"),
        ("Tangible assets", "11"),
        ("fixed_total", None),
        ("Current assets", None),
        ("Stocks", "12"),
        ("Debtors", "13"),
        ("Cash at bank and in hand", None),
        ("current_total", None),
        ("Creditors: amounts falling due within one year", "14"),
        ("Net current assets", None),
        ("Total assets less current liabilities", None),
        ("Provisions for liabilities", "15"),
        ("Net assets", None),
        ("Capital and reserves", None),
        ("Called up share capital", "16"),
        ("Profit and loss account", None),
        ("Total shareholders' funds", None),
    ]
    for label, note in order:
        items = []
        if not label.endswith("_total"):
            items.append(L(60, label))
        if note:
            items.append(L(335, note))
        if label in v:
            a, b = v[label]
            items += [R(440, a), R(520, b)]
        lines.append((yy, items))
        yy += 15
    return lines
