#!/usr/bin/env python3
"""
Companies House Financial Data Extractor - Main CLI
"""

import os
import sys
import click
from dotenv import load_dotenv
from pathlib import Path

from src.api_client import CompaniesHouseAPIClient
from src.pdf_extractor import PDFExtractor
from src.financial_parser import FinancialDataParser


# Load environment variables
load_dotenv()


def get_api_key() -> str:
    """Get API key from environment or prompt user"""
    api_key = os.getenv('COMPANIES_HOUSE_API_KEY')

    if not api_key:
        click.echo("Error: COMPANIES_HOUSE_API_KEY not found in environment variables", err=True)
        click.echo("Please set it in your .env file or export it:", err=True)
        click.echo("  export COMPANIES_HOUSE_API_KEY=your_key_here", err=True)
        sys.exit(1)

    return api_key


@click.group()
@click.version_option(version="0.1.0")
def cli():
    """Companies House Financial Data Extractor

    Extract financial information from Companies House PDF accounts.
    """
    pass


@cli.command()
@click.argument('company_number')
@click.option('--transaction-id', '-t', help='Specific transaction ID to extract')
@click.option('--format', '-f', type=click.Choice(['table', 'json', 'csv']), default='table',
              help='Output format')
@click.option('--output', '-o', type=click.Path(), help='Output file path')
@click.option('--filter', '-F', type=click.Choice(['all', 'balance-sheet', 'profit-loss']),
              default='all', help='Filter financial items')
@click.option('--save-pdf', type=click.Path(), help='Save downloaded PDF to file')
def extract(company_number, transaction_id, format, output, filter, save_pdf):
    """Extract financial data from a company's accounts

    COMPANY_NUMBER: The company registration number (e.g., 01234567)
    """
    api_key = get_api_key()
    client = CompaniesHouseAPIClient(api_key)

    try:
        # Get company profile
        click.echo(f"Fetching company profile for {company_number}...")
        profile = client.get_company_profile(company_number)
        click.echo(f"Company: {profile.get('company_name', 'Unknown')}")

        # Get filing or use specific transaction ID
        if transaction_id:
            click.echo(f"Using transaction ID: {transaction_id}")
        else:
            click.echo("Finding latest PDF accounts...")
            latest_filing = client.get_latest_pdf_account(company_number)

            if not latest_filing:
                click.echo("Error: No PDF accounts found for this company", err=True)
                click.echo("The company may only have XBRL filings or no accounts yet", err=True)
                sys.exit(1)

            transaction_id = client.extract_transaction_id(latest_filing)
            if not transaction_id:
                click.echo("Error: Could not extract transaction ID from filing", err=True)
                sys.exit(1)

            filing_date = latest_filing.get('date', 'Unknown')
            filing_desc = latest_filing.get('description', 'Unknown')
            click.echo(f"Found filing: {filing_desc} (Date: {filing_date})")

        # Download PDF
        click.echo(f"Downloading PDF (Transaction ID: {transaction_id})...")
        pdf_content = client.download_document(transaction_id, output_path=save_pdf)
        click.echo(f"Downloaded {len(pdf_content)} bytes")

        # Extract text and financial data
        click.echo("Extracting financial data from PDF...")
        extractor = PDFExtractor(pdf_content)
        extractor.extract_text()

        # Get company info from PDF
        company_info = extractor.extract_company_info()
        click.echo(f"PDF Pages: {extractor.get_page_count()}")

        # Extract financial items
        financial_items = extractor.extract_financial_lines()
        click.echo(f"Extracted {len(financial_items)} financial line items")

        if not financial_items:
            click.echo("Warning: No financial data could be extracted from the PDF", err=True)
            click.echo("This may be a scanned PDF or have an unusual format", err=True)
            sys.exit(1)

        # Create parser and apply filters
        parser = FinancialDataParser(financial_items, company_info)

        if filter == 'balance-sheet':
            parser = parser.get_balance_sheet_items()
            click.echo(f"Filtered to {len(parser.financial_items)} balance sheet items")
        elif filter == 'profit-loss':
            parser = parser.get_profit_loss_items()
            click.echo(f"Filtered to {len(parser.financial_items)} P&L items")

        # Format output
        if format == 'table':
            result = parser.to_table()
        elif format == 'json':
            result = parser.to_json()
        elif format == 'csv':
            result = parser.to_csv()

        # Output results
        if output:
            with open(output, 'w') as f:
                f.write(result)
            click.echo(f"\nResults saved to: {output}")
        else:
            click.echo("\n" + "="*80)
            click.echo(result)
            click.echo("="*80)

    except Exception as e:
        click.echo(f"Error: {str(e)}", err=True)
        sys.exit(1)


@cli.command()
@click.argument('company_number')
@click.option('--limit', '-l', default=10, help='Number of filings to show')
def list(company_number, limit):
    """List available accounts for a company

    COMPANY_NUMBER: The company registration number (e.g., 01234567)
    """
    api_key = get_api_key()
    client = CompaniesHouseAPIClient(api_key)

    try:
        # Get company profile
        profile = client.get_company_profile(company_number)
        click.echo(f"Company: {profile.get('company_name', 'Unknown')}")
        click.echo(f"Number: {company_number}")
        click.echo(f"Status: {profile.get('company_status', 'Unknown')}\n")

        # Get filing history
        click.echo("Fetching filing history...")
        pdf_accounts = client.get_pdf_accounts(company_number)

        if not pdf_accounts:
            click.echo("No PDF accounts found for this company")
            return

        click.echo(f"Found {len(pdf_accounts)} PDF account filings:\n")

        # Display filings
        for i, filing in enumerate(pdf_accounts[:limit], 1):
            date = filing.get('date', 'Unknown')
            description = filing.get('description', 'Unknown')
            transaction_id = client.extract_transaction_id(filing)

            click.echo(f"{i}. {description}")
            click.echo(f"   Date: {date}")
            click.echo(f"   Transaction ID: {transaction_id}")
            click.echo()

    except Exception as e:
        click.echo(f"Error: {str(e)}", err=True)
        sys.exit(1)


@cli.command()
@click.argument('company_number')
def info(company_number):
    """Get company information

    COMPANY_NUMBER: The company registration number (e.g., 01234567)
    """
    api_key = get_api_key()
    client = CompaniesHouseAPIClient(api_key)

    try:
        profile = client.get_company_profile(company_number)

        click.echo("\nCompany Information:")
        click.echo("="*50)
        click.echo(f"Name: {profile.get('company_name', 'Unknown')}")
        click.echo(f"Number: {company_number}")
        click.echo(f"Status: {profile.get('company_status', 'Unknown')}")
        click.echo(f"Type: {profile.get('type', 'Unknown')}")
        click.echo(f"Incorporated: {profile.get('date_of_creation', 'Unknown')}")

        if 'registered_office_address' in profile:
            addr = profile['registered_office_address']
            click.echo(f"\nRegistered Office:")
            for key in ['address_line_1', 'address_line_2', 'locality', 'postal_code']:
                if key in addr and addr[key]:
                    click.echo(f"  {addr[key]}")

        click.echo("="*50 + "\n")

    except Exception as e:
        click.echo(f"Error: {str(e)}", err=True)
        sys.exit(1)


if __name__ == '__main__':
    cli()
