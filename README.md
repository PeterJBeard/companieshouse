# Companies House Financial Data Extractor

A Python application that extracts financial information from Companies House PDF accounts.

## Features

- Fetch PDF accounts from Companies House API
- Download PDFs from S3-hosted URLs
- Extract balance sheet and financial data
- Output structured financial information

## Setup

1. **Clone the repository**
   ```bash
   git clone <repository-url>
   cd companieshouse
   ```

2. **Create a virtual environment**
   ```bash
   python3 -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure API Key**
   - Copy `.env.example` to `.env`
   - Get your API key from [Companies House Developer Hub](https://developer.company-information.service.gov.uk)
   - Add your API key to `.env`:
     ```
     COMPANIES_HOUSE_API_KEY=your_api_key_here
     ```

## Usage

### Extract financial data from a company

```bash
python main.py extract 01234567
```

### List available accounts for a company

```bash
python main.py list 01234567
```

### Extract from a specific filing

```bash
python main.py extract 01234567 --transaction-id MzM2NjExMTAyM2FkaXF6a2N4
```

### Output formats

```bash
# JSON output
python main.py extract 01234567 --format json

# CSV output
python main.py extract 01234567 --format csv

# Table output (default)
python main.py extract 01234567 --format table
```

## API Information

The app uses the Companies House API:
- **Base URL**: https://api.company-information.service.gov.uk
- **Document API**: https://document-api.companieshouse.gov.uk
- **Rate Limit**: 600 requests per 5 minutes
- **Authentication**: Basic auth with API key as username (no password)

## How It Works

1. **Fetch Filing History**: Queries the Companies House API for a company's filing history
2. **Filter PDF Accounts**: Finds accounts filed as PDFs (type "AA", non-XBRL)
3. **Download PDF**: Downloads the PDF from the S3-hosted URL
4. **Extract Data**: Parses the PDF and extracts financial line items
5. **Structure Output**: Formats the data as tables, JSON, or CSV

## Limitations

- Works best with text-based PDFs (not scanned images)
- May struggle with complex PDF layouts
- Requires valid Companies House API key
- S3 URLs expire after 60 seconds (automatically refreshed)

## License

MIT
