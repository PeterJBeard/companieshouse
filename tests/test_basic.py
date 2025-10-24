#!/usr/bin/env python3
"""
Basic tests for the Companies House extractor
"""

import os
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.api_client import CompaniesHouseAPIClient
from src.pdf_extractor import PDFExtractor
from src.financial_parser import FinancialDataParser


def test_api_client():
    """Test API client initialization"""
    print("Testing API client...")

    api_key = os.getenv('COMPANIES_HOUSE_API_KEY', 'test_key')
    client = CompaniesHouseAPIClient(api_key)

    assert client.api_key == api_key
    assert client.BASE_URL == "https://api.company-information.service.gov.uk"
    print("✓ API client initialization works")


def test_transaction_id_extraction():
    """Test transaction ID extraction"""
    print("Testing transaction ID extraction...")

    api_key = os.getenv('COMPANIES_HOUSE_API_KEY', 'test_key')
    client = CompaniesHouseAPIClient(api_key)

    # Mock filing data
    filing = {
        'links': {
            'document_metadata': '/document/abc123xyz/metadata'
        }
    }

    transaction_id = client.extract_transaction_id(filing)
    assert transaction_id == 'abc123xyz'
    print("✓ Transaction ID extraction works")


def test_financial_parser():
    """Test financial parser"""
    print("Testing financial parser...")

    # Mock financial data
    financial_items = [
        {
            'item': 'Cash at bank',
            'current_year': 100000.0,
            'previous_year': 90000.0,
            'currency': 'GBP'
        },
        {
            'item': 'Trade debtors',
            'current_year': 50000.0,
            'previous_year': 45000.0,
            'currency': 'GBP'
        }
    ]

    company_info = {
        'company_name': 'TEST COMPANY LIMITED',
        'company_number': '00000000'
    }

    parser = FinancialDataParser(financial_items, company_info)

    # Test table output
    table = parser.to_table()
    assert 'Cash at bank' in table
    assert 'TEST COMPANY LIMITED' in table
    print("✓ Table formatting works")

    # Test JSON output
    json_output = parser.to_json()
    assert '"item": "Cash at bank"' in json_output
    print("✓ JSON formatting works")

    # Test CSV output
    csv_output = parser.to_csv()
    assert 'Cash at bank' in csv_output
    print("✓ CSV formatting works")

    # Test summary
    data = parser.to_dict()
    summary = data['summary']
    assert summary['total_items'] == 2
    assert summary['total_current_year'] == 150000.0
    print("✓ Summary calculation works")


def test_filtering():
    """Test financial data filtering"""
    print("Testing filtering...")

    financial_items = [
        {'item': 'Cash at bank', 'current_year': 100000, 'previous_year': 90000, 'currency': 'GBP'},
        {'item': 'Total assets', 'current_year': 500000, 'previous_year': 450000, 'currency': 'GBP'},
        {'item': 'Turnover', 'current_year': 1000000, 'previous_year': 950000, 'currency': 'GBP'},
        {'item': 'Operating profit', 'current_year': 200000, 'previous_year': 180000, 'currency': 'GBP'},
    ]

    parser = FinancialDataParser(financial_items)

    # Test balance sheet filter
    bs_parser = parser.get_balance_sheet_items()
    assert len(bs_parser.financial_items) == 2  # Cash and assets
    print("✓ Balance sheet filtering works")

    # Test P&L filter
    pl_parser = parser.get_profit_loss_items()
    assert len(pl_parser.financial_items) == 2  # Turnover and profit
    print("✓ P&L filtering works")


def run_tests():
    """Run all tests"""
    print("Running basic tests...\n")

    try:
        test_api_client()
        test_transaction_id_extraction()
        test_financial_parser()
        test_filtering()

        print("\n" + "="*50)
        print("All tests passed! ✓")
        print("="*50)

    except AssertionError as e:
        print(f"\n✗ Test failed: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n✗ Error: {e}")
        sys.exit(1)


if __name__ == '__main__':
    run_tests()
