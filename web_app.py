#!/usr/bin/env python3
"""
Simple Web UI for Companies House Financial Data Extractor
No technical knowledge required - just open in your browser!
"""

import os
import sys
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_file
from dotenv import load_dotenv
import io
import json

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent))

from src.api_client import CompaniesHouseAPIClient
from src.pdf_extractor import PDFExtractor
from src.financial_parser import FinancialDataParser

# Load environment variables
load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = 'dev-key-for-companies-house-app'


def get_api_key():
    """Get API key from environment"""
    return os.getenv('COMPANIES_HOUSE_API_KEY')


@app.route('/')
def index():
    """Main page"""
    api_key = get_api_key()
    has_api_key = bool(api_key)
    return render_template('index.html', has_api_key=has_api_key)


@app.route('/api/company-info', methods=['POST'])
def company_info():
    """Get company information"""
    try:
        company_number = request.json.get('company_number', '').strip()
        if not company_number:
            return jsonify({'error': 'Please enter a company number'}), 400

        api_key = get_api_key()
        if not api_key:
            return jsonify({'error': 'API key not configured'}), 500

        client = CompaniesHouseAPIClient(api_key)
        profile = client.get_company_profile(company_number)

        return jsonify({
            'success': True,
            'company_name': profile.get('company_name', 'Unknown'),
            'company_number': company_number,
            'status': profile.get('company_status', 'Unknown'),
            'type': profile.get('type', 'Unknown')
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/list-accounts', methods=['POST'])
def list_accounts():
    """List available accounts for a company"""
    try:
        company_number = request.json.get('company_number', '').strip()
        if not company_number:
            return jsonify({'error': 'Please enter a company number'}), 400

        api_key = get_api_key()
        client = CompaniesHouseAPIClient(api_key)

        pdf_accounts = client.get_pdf_accounts(company_number)

        accounts_list = []
        for filing in pdf_accounts[:10]:  # Limit to 10 most recent
            transaction_id = client.extract_transaction_id(filing)
            accounts_list.append({
                'date': filing.get('date', 'Unknown'),
                'description': filing.get('description', 'Unknown'),
                'transaction_id': transaction_id
            })

        return jsonify({
            'success': True,
            'accounts': accounts_list,
            'count': len(accounts_list)
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/extract-financials', methods=['POST'])
def extract_financials():
    """Extract financial data from accounts"""
    try:
        company_number = request.json.get('company_number', '').strip()
        transaction_id = request.json.get('transaction_id', '').strip()
        filter_type = request.json.get('filter', 'all')

        if not company_number:
            return jsonify({'error': 'Please enter a company number'}), 400

        api_key = get_api_key()
        client = CompaniesHouseAPIClient(api_key)

        # Get transaction ID if not provided
        if not transaction_id:
            latest_filing = client.get_latest_pdf_account(company_number)
            if not latest_filing:
                return jsonify({'error': 'No PDF accounts found for this company'}), 404
            transaction_id = client.extract_transaction_id(latest_filing)

        # Download PDF
        pdf_content = client.download_document(transaction_id)

        # Extract financial data
        extractor = PDFExtractor(pdf_content)
        extractor.extract_text()
        company_info = extractor.extract_company_info()
        financial_items = extractor.extract_financial_lines()

        if not financial_items:
            return jsonify({'error': 'No financial data could be extracted from this PDF'}), 404

        # Create parser
        parser = FinancialDataParser(financial_items, company_info)

        # Apply filter
        if filter_type == 'balance-sheet':
            parser = parser.get_balance_sheet_items()
        elif filter_type == 'profit-loss':
            parser = parser.get_profit_loss_items()

        # Format data for display
        data = parser.to_dict()

        # Format financial items for table display
        formatted_items = []
        for item in data['financial_items']:
            current = item['current_year']
            previous = item['previous_year']
            change = current - previous
            change_pct = ((change / previous) * 100) if previous != 0 else 0

            formatted_items.append({
                'item': item['item'],
                'current_year': f"£{current:,.2f}",
                'previous_year': f"£{previous:,.2f}",
                'change': f"£{change:,.2f}",
                'change_pct': f"{change_pct:+.2f}%"
            })

        return jsonify({
            'success': True,
            'company_info': data['company_info'],
            'financial_items': formatted_items,
            'summary': data['summary']
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/download-csv', methods=['POST'])
def download_csv():
    """Download financial data as CSV"""
    try:
        data = request.json.get('financial_items', [])

        # Create CSV
        csv_lines = ['Item,Current Year (£),Previous Year (£),Change (£),Change (%)']
        for item in data:
            csv_lines.append(f"{item['item']},{item['current_year']},{item['previous_year']},{item['change']},{item['change_pct']}")

        csv_content = '\n'.join(csv_lines)

        return send_file(
            io.BytesIO(csv_content.encode()),
            mimetype='text/csv',
            as_attachment=True,
            download_name='financial_data.csv'
        )

    except Exception as e:
        return jsonify({'error': str(e)}), 400


if __name__ == '__main__':
    print("\n" + "="*60)
    print("Companies House Financial Data Extractor")
    print("="*60)
    print("\nStarting web interface...")
    print("\nOpen your web browser and go to:")
    print("\n    http://localhost:5000")
    print("\nPress Ctrl+C to stop the server")
    print("="*60 + "\n")

    app.run(debug=True, host='0.0.0.0', port=5000)
