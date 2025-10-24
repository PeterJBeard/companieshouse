#!/usr/bin/env python3
"""
Example script showing how to use the Companies House extractor programmatically
"""

import os
import sys
from pathlib import Path

# Add parent directory to path to import our modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.api_client import CompaniesHouseAPIClient
from src.pdf_extractor import PDFExtractor
from src.financial_parser import FinancialDataParser


def main():
    """Example usage of the extractor"""

    # Get API key from environment
    api_key = os.getenv('COMPANIES_HOUSE_API_KEY')
    if not api_key:
        print("Error: Set COMPANIES_HOUSE_API_KEY environment variable")
        sys.exit(1)

    # Initialize client
    client = CompaniesHouseAPIClient(api_key)

    # Example company number (change this to test with different companies)
    company_number = "00000006"

    print(f"Fetching data for company {company_number}...")

    # Get company profile
    profile = client.get_company_profile(company_number)
    print(f"Company: {profile.get('company_name')}")

    # Get latest PDF account
    latest_filing = client.get_latest_pdf_account(company_number)
    if not latest_filing:
        print("No PDF accounts found")
        sys.exit(1)

    print(f"Latest filing: {latest_filing.get('description')}")
    print(f"Date: {latest_filing.get('date')}")

    # Extract transaction ID
    transaction_id = client.extract_transaction_id(latest_filing)
    print(f"Transaction ID: {transaction_id}")

    # Download PDF
    print("Downloading PDF...")
    pdf_content = client.download_document(transaction_id)
    print(f"Downloaded {len(pdf_content)} bytes")

    # Extract data from PDF
    print("Extracting financial data...")
    extractor = PDFExtractor(pdf_content)
    text = extractor.extract_text()

    # Get company info
    company_info = extractor.extract_company_info()
    print(f"Company info from PDF: {company_info}")

    # Extract financial items
    financial_items = extractor.extract_financial_lines()
    print(f"Found {len(financial_items)} financial line items")

    # Create parser
    parser = FinancialDataParser(financial_items, company_info)

    # Display as table
    print("\n" + "="*80)
    print(parser.to_table())
    print("="*80)

    # Get balance sheet items only
    balance_sheet = parser.get_balance_sheet_items()
    print(f"\nBalance sheet has {len(balance_sheet.financial_items)} items")

    # Get profit & loss items only
    pl_items = parser.get_profit_loss_items()
    print(f"P&L has {len(pl_items.financial_items)} items")

    # Export to JSON
    json_output = parser.to_json()
    print("\nJSON output (first 500 chars):")
    print(json_output[:500] + "...")

    # Get summary
    data = parser.to_dict()
    summary = data['summary']
    print("\nSummary:")
    print(f"  Total items: {summary['total_items']}")
    print(f"  Current year total: £{summary['total_current_year']:,.2f}")
    print(f"  Previous year total: £{summary['total_previous_year']:,.2f}")
    print(f"  Change: £{summary['total_change']:,.2f} ({summary['total_change_pct']:+.2f}%)")


if __name__ == '__main__':
    main()
