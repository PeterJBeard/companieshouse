"""Companies House API Client"""

import requests
import base64
from typing import Dict, List, Optional
import time


class CompaniesHouseAPIClient:
    """Client for interacting with Companies House API"""

    BASE_URL = "https://api.company-information.service.gov.uk"
    DOCUMENT_API_URL = "https://document-api.companieshouse.gov.uk"

    def __init__(self, api_key: str):
        """
        Initialize the API client

        Args:
            api_key: Companies House API key
        """
        self.api_key = api_key
        self.session = requests.Session()

        # Companies House uses Basic Auth with API key as username and blank password
        auth_string = f"{api_key}:"
        encoded_auth = base64.b64encode(auth_string.encode()).decode()
        self.session.headers.update({
            'Authorization': f'Basic {encoded_auth}',
            'User-Agent': 'CompaniesHouseExtractor/0.1.0'
        })

    def get_company_profile(self, company_number: str) -> Dict:
        """
        Get company profile information

        Args:
            company_number: Company registration number

        Returns:
            Company profile data
        """
        url = f"{self.BASE_URL}/company/{company_number}"
        response = self.session.get(url)
        response.raise_for_status()
        return response.json()

    def get_filing_history(
        self,
        company_number: str,
        category: str = "accounts",
        items_per_page: int = 50
    ) -> List[Dict]:
        """
        Get filing history for a company

        Args:
            company_number: Company registration number
            category: Filter by category (default: accounts)
            items_per_page: Number of items per page

        Returns:
            List of filing history items
        """
        url = f"{self.BASE_URL}/company/{company_number}/filing-history"
        params = {
            'category': category,
            'items_per_page': items_per_page
        }

        response = self.session.get(url, params=params)
        response.raise_for_status()
        data = response.json()

        return data.get('items', [])

    def get_pdf_accounts(self, company_number: str) -> List[Dict]:
        """
        Get PDF accounts (non-XBRL) for a company

        Args:
            company_number: Company registration number

        Returns:
            List of PDF account filings
        """
        filings = self.get_filing_history(company_number)
        pdf_accounts = []

        for filing in filings:
            # Look for accounts type "AA" which are typically full accounts
            if filing.get('type') == 'AA' and filing.get('category') == 'accounts':
                # Check if document metadata is available
                links = filing.get('links', {})
                if 'document_metadata' in links:
                    pdf_accounts.append(filing)

        return pdf_accounts

    def get_document_metadata(self, document_id: str) -> Dict:
        """
        Get document metadata to check if PDF is available

        Args:
            document_id: Document/transaction ID

        Returns:
            Document metadata
        """
        # Extract transaction ID from the document_metadata link
        url = f"https://frontend-doc-api.company-information.service.gov.uk/document/{document_id}"
        response = self.session.get(url)
        response.raise_for_status()
        return response.json()

    def download_document(
        self,
        transaction_id: str,
        output_path: Optional[str] = None
    ) -> bytes:
        """
        Download a document from Companies House

        Args:
            transaction_id: Transaction ID for the document
            output_path: Optional path to save the PDF

        Returns:
            PDF content as bytes
        """
        url = f"{self.DOCUMENT_API_URL}/document/{transaction_id}/content"
        headers = {
            'Accept': 'application/pdf'
        }

        # Use the same auth as regular API
        response = self.session.get(url, headers=headers)
        response.raise_for_status()

        pdf_content = response.content

        if output_path:
            with open(output_path, 'wb') as f:
                f.write(pdf_content)

        return pdf_content

    def get_latest_pdf_account(self, company_number: str) -> Optional[Dict]:
        """
        Get the latest PDF account for a company

        Args:
            company_number: Company registration number

        Returns:
            Latest PDF account filing or None
        """
        pdf_accounts = self.get_pdf_accounts(company_number)
        if pdf_accounts:
            return pdf_accounts[0]  # Most recent first
        return None

    def extract_transaction_id(self, filing: Dict) -> Optional[str]:
        """
        Extract transaction ID from a filing record

        Args:
            filing: Filing history item

        Returns:
            Transaction ID or None
        """
        links = filing.get('links', {})
        doc_metadata = links.get('document_metadata', '')

        # Extract transaction ID from URL like:
        # /document/abc123xyz/metadata
        if doc_metadata:
            parts = doc_metadata.split('/')
            if len(parts) >= 3:
                return parts[2]  # Get the transaction ID

        return None
