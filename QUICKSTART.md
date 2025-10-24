# Quick Start Guide

## Installation

1. **Clone and setup**
   ```bash
   git clone <repo-url>
   cd companieshouse
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Configure API Key**
   ```bash
   cp .env.example .env
   # Edit .env and add your Companies House API key
   ```

   Get your free API key from: https://developer.company-information.service.gov.uk

## Basic Usage

### Get company information
```bash
python main.py info 00000006
```

### List available accounts
```bash
python main.py list 00000006
```

### Extract financial data
```bash
# Extract from latest accounts (table format)
python main.py extract 00000006

# Save as JSON
python main.py extract 00000006 --format json --output financial_data.json

# Save as CSV
python main.py extract 00000006 --format csv --output financial_data.csv

# Filter to balance sheet items only
python main.py extract 00000006 --filter balance-sheet

# Save the downloaded PDF
python main.py extract 00000006 --save-pdf accounts.pdf
```

### Extract from a specific filing
```bash
# First, list available filings to get transaction IDs
python main.py list 00000006

# Then extract using the transaction ID
python main.py extract 00000006 --transaction-id MzM2NjExMTAyM2FkaXF6a2N4
```

## Example Output

### Table Format (default)
```
Company: EXAMPLE COMPANY LIMITED
Number: 00000006
Period End: 31/12/2023

+----------------------+---------------------+----------------------+---------------+-------------+
| Item                 | Current Year (£)    | Previous Year (£)    | Change (£)    | Change (%)  |
+======================+=====================+======================+===============+=============+
| Cash at bank         | 100,000.00         | 90,000.00           | 10,000.00     | +11.11%     |
+----------------------+---------------------+----------------------+---------------+-------------+
| Trade debtors        | 50,000.00          | 45,000.00           | 5,000.00      | +11.11%     |
+----------------------+---------------------+----------------------+---------------+-------------+
| Total assets         | 250,000.00         | 230,000.00          | 20,000.00     | +8.70%      |
+----------------------+---------------------+----------------------+---------------+-------------+
```

### JSON Format
```json
{
  "company_info": {
    "company_name": "EXAMPLE COMPANY LIMITED",
    "company_number": "00000006",
    "period_end": "31/12/2023"
  },
  "financial_items": [
    {
      "item": "Cash at bank",
      "current_year": 100000.0,
      "previous_year": 90000.0,
      "currency": "GBP"
    }
  ],
  "summary": {
    "total_items": 15,
    "total_current_year": 500000.0,
    "total_previous_year": 450000.0
  }
}
```

## Troubleshooting

### API Key Issues
- Error: "COMPANIES_HOUSE_API_KEY not found"
  - Make sure you've created a `.env` file with your API key
  - Check that you've loaded the environment: `source venv/bin/activate`

### No PDF Accounts Found
- Some companies only file XBRL (XML) accounts, not PDFs
- Try a different company or check Companies House website manually

### Extraction Issues
- If no financial data is extracted, the PDF may be:
  - A scanned image (poor OCR quality)
  - Using an unusual layout
  - Not containing standard financial tables

Try saving the PDF with `--save-pdf` and manually inspect it.

## Common Company Numbers for Testing

- `00000006` - One of the oldest UK companies
- `09252748` - A typical small limited company
- `00445790` - Marks and Spencer Group PLC (large company)

Note: Not all companies have PDF accounts available - some only file XBRL format.
