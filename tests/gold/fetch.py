"""Download the gold-set filings from the public Companies House service (no API key needed).

PDFs are not committed (.gitignore excludes *.pdf); each download is checked against the
SHA-256 pinned in its gold JSON so the bench always runs on the exact filed document.

    python -m tests.gold.fetch            # into tests/gold/pdfs/
    python -m tests.gold.fetch --dir /tmp/gold
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from ch_accounts_mcp.companies_house import fetch_url, public_document_url

GOLD_DIR = Path(__file__).resolve().parent
DEFAULT_PDF_DIR = GOLD_DIR / "pdfs"


def gold_files() -> list[Path]:
    return sorted(GOLD_DIR.glob("*.json"))


def load_gold(path: Path) -> dict:
    return json.loads(path.read_text())


def pdf_path_for(gold: dict, pdf_dir: Path = DEFAULT_PDF_DIR) -> Path:
    return pdf_dir / f"{gold['company_number']}_{gold['transaction_id']}.pdf"


def ensure_pdf(gold: dict, pdf_dir: Path = DEFAULT_PDF_DIR) -> Path:
    path = pdf_path_for(gold, pdf_dir)
    if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == gold["sha256"]:
        return path
    data = fetch_url(public_document_url(gold["company_number"], gold["transaction_id"]))
    got = hashlib.sha256(data).hexdigest()
    if got != gold["sha256"]:
        raise RuntimeError(f"{gold['name']}: downloaded PDF hash {got[:12]}… does not match the pinned {gold['sha256'][:12]}…")
    pdf_dir.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dir", type=Path, default=DEFAULT_PDF_DIR)
    args = ap.parse_args()
    for g in gold_files():
        gold = load_gold(g)
        print(f"{ensure_pdf(gold, args.dir)}  ({gold['name']})")


if __name__ == "__main__":
    main()
