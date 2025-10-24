# Companies House API Details

## Overview

This document provides technical details about the Companies House API integration.

## API Endpoints

### 1. Company Profile
```
GET https://api.company-information.service.gov.uk/company/{company_number}
```

Returns basic company information including name, status, type, and registered office.

**Example:**
```bash
curl -u YOUR_API_KEY: https://api.company-information.service.gov.uk/company/00000006
```

### 2. Filing History
```
GET https://api.company-information.service.gov.uk/company/{company_number}/filing-history
```

**Parameters:**
- `category` - Filter by category (e.g., "accounts")
- `items_per_page` - Number of items to return (max 100)

**Example:**
```bash
curl -u YOUR_API_KEY: \
  "https://api.company-information.service.gov.uk/company/00000006/filing-history?category=accounts&items_per_page=50"
```

### 3. Document Metadata
```
GET https://frontend-doc-api.company-information.service.gov.uk/document/{transaction_id}
```

Returns metadata about a document, including available formats (PDF, XHTML, etc.)

### 4. Document Content
```
GET https://document-api.companieshouse.gov.uk/document/{transaction_id}/content
```

**Headers:**
- `Accept: application/pdf` - Request PDF format
- `Authorization: Basic {api_key}` - Same auth as other endpoints

Downloads the actual document content. Returns S3-hosted PDF with temporary credentials.

## Authentication

Companies House API uses HTTP Basic Authentication:
- **Username:** Your API key
- **Password:** (empty string)

**Python example:**
```python
import base64
api_key = "your_api_key_here"
auth_string = f"{api_key}:"
encoded = base64.b64encode(auth_string.encode()).decode()
headers = {'Authorization': f'Basic {encoded}'}
```

## Rate Limits

- **600 requests per 5 minutes** per API key
- If exceeded, you'll receive HTTP 429 (Too Many Requests)
- Consider implementing exponential backoff for retries

## Filing Types

Common filing type codes:
- `AA` - Annual accounts (full accounts)
- `CS01` - Confirmation statement
- `AP01` - Appointment of director
- `TM01` - Termination of director

For PDF extraction, we focus on type `AA` filings.

## XBRL vs PDF Accounts

Companies can file accounts in two formats:

### XBRL (iXBRL)
- XML-based format
- Machine-readable
- Structured data
- Content-type: `application/xhtml+xml`
- **Not supported by this tool** (use XML parsing instead)

### PDF
- Human-readable documents
- Requires text extraction
- Content-type: `application/pdf`
- **Supported by this tool**

To check if a filing is PDF:
```python
# Check document metadata
metadata = client.get_document_metadata(transaction_id)
resources = metadata.get('resources', {})

# Look for PDF resource
has_pdf = 'application/pdf' in resources
has_xbrl = 'application/xhtml+xml' in resources
```

## S3 URLs

Document content is hosted on AWS S3:
```
https://s3.eu-west-2.amazonaws.com/document-api-images-live.ch.gov.uk/docs/...
```

**Important notes:**
- URLs include temporary AWS credentials (`X-Amz-*` parameters)
- Credentials expire after **60 seconds**
- Always fetch fresh URLs via the API
- Don't cache or store S3 URLs

## Error Handling

Common errors:

### 401 Unauthorized
- Invalid API key
- Check your API key is correct
- Ensure proper Basic Auth encoding

### 404 Not Found
- Company number doesn't exist
- Document/transaction ID doesn't exist
- Check the company number format (8 digits, zero-padded)

### 429 Too Many Requests
- Rate limit exceeded
- Wait 5 minutes before retrying
- Implement request throttling

### 500 Internal Server Error
- Companies House API issue
- Retry with exponential backoff
- Check Companies House status page

## Company Number Format

Company numbers must be:
- Exactly 8 characters
- Padded with leading zeros
- All uppercase for letters (NI companies)

**Examples:**
- `00000006` ✓ (correct)
- `6` ✗ (needs padding)
- `000006` ✗ (too short)
- `SC123456` ✓ (Scottish company)
- `NI123456` ✓ (Northern Irish company)

## Pagination

When dealing with filing history:

```python
# Get all filings (handle pagination)
all_filings = []
start_index = 0
items_per_page = 100

while True:
    response = get_filing_history(
        company_number,
        start_index=start_index,
        items_per_page=items_per_page
    )

    items = response.get('items', [])
    all_filings.extend(items)

    total = response.get('total_count', 0)
    if start_index + len(items) >= total:
        break

    start_index += items_per_page
```

## Testing

Test companies with various characteristics:

1. **00000006** - Very old company, good for testing
2. **09252748** - Typical small limited company
3. **00445790** - Large PLC (Marks and Spencer)

Always test with real company numbers, as Companies House provides a live production API only (no sandbox).

## Resources

- [Companies House Developer Hub](https://developer.company-information.service.gov.uk)
- [API Reference](https://developer-specs.company-information.service.gov.uk/companies-house-public-data-api/reference)
- [Get API Key](https://developer.company-information.service.gov.uk/get-started)
- [API Status](https://status.companieshouse.gov.uk/)

## Best Practices

1. **Cache company profiles** - They don't change frequently
2. **Respect rate limits** - Implement throttling
3. **Handle errors gracefully** - Use retries with backoff
4. **Validate company numbers** - Before making API calls
5. **Store PDFs locally** - Don't re-download unnecessarily
6. **Use specific transaction IDs** - When you know which filing you want
7. **Monitor API usage** - Track your request count
