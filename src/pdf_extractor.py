"""PDF Extraction and Parsing"""

import pdfplumber
import re
from typing import Dict, List, Optional, Tuple
from io import BytesIO


class PDFExtractor:
    """Extract text and data from PDF documents"""

    def __init__(self, pdf_content: bytes):
        """
        Initialize PDF extractor

        Args:
            pdf_content: PDF file content as bytes
        """
        self.pdf_content = pdf_content
        self.text = None
        self.pages = []

    def extract_text(self) -> str:
        """
        Extract all text from the PDF

        Returns:
            Full text content of the PDF
        """
        if self.text is not None:
            return self.text

        text_parts = []

        try:
            with pdfplumber.open(BytesIO(self.pdf_content)) as pdf:
                for page in pdf.pages:
                    page_text = page.extract_text()
                    if page_text:
                        text_parts.append(page_text)
                        self.pages.append(page_text)
        except Exception as e:
            raise ValueError(f"Failed to extract text from PDF: {str(e)}")

        self.text = "\n\n".join(text_parts)
        return self.text

    def extract_tables(self) -> List[List[List[str]]]:
        """
        Extract tables from the PDF

        Returns:
            List of tables, where each table is a list of rows
        """
        all_tables = []

        try:
            with pdfplumber.open(BytesIO(self.pdf_content)) as pdf:
                for page in pdf.pages:
                    tables = page.extract_tables()
                    if tables:
                        all_tables.extend(tables)
        except Exception as e:
            raise ValueError(f"Failed to extract tables from PDF: {str(e)}")

        return all_tables

    def find_financial_section(self, section_name: str) -> Optional[str]:
        """
        Find a specific financial section in the PDF

        Args:
            section_name: Name of the section (e.g., "Balance Sheet", "Profit and Loss")

        Returns:
            Text of the section or None if not found
        """
        if self.text is None:
            self.extract_text()

        # Look for section headings (case-insensitive)
        pattern = re.compile(
            rf"({re.escape(section_name)}.*?)(?=\n\n[A-Z]|\Z)",
            re.IGNORECASE | re.DOTALL
        )

        match = pattern.search(self.text)
        if match:
            return match.group(1).strip()

        return None

    def extract_financial_lines(self) -> List[Dict[str, any]]:
        """
        Extract financial line items from the PDF.
        Looks for patterns like:
        "Item Name    £current    £previous"
        "Trade Creditors    57,054    62,853"

        Returns:
            List of financial line items with item name and values
        """
        if self.text is None:
            self.extract_text()

        financial_items = []

        # Pattern to match financial lines with currency values
        # Matches lines like: "Item Name    £123,456    £234,567" or "Item    123,456    234,567"
        patterns = [
            # With £ symbols
            r'([A-Za-z][A-Za-z\s\-/()]+?)\s+£?([\d,]+)\s+£?([\d,]+)',
            # Table-style with clear columns
            r'([A-Za-z][A-Za-z\s\-/()]+?)\s{2,}([\d,]+)\s{2,}([\d,]+)',
            # With parentheses for negatives
            r'([A-Za-z][A-Za-z\s\-/()]+?)\s+£?\(?([\d,]+)\)?\s+£?\(?([\d,]+)\)?',
        ]

        for line in self.text.split('\n'):
            line = line.strip()
            if not line:
                continue

            for pattern in patterns:
                match = re.search(pattern, line)
                if match:
                    item_name = match.group(1).strip()
                    current_value = match.group(2).replace(',', '')
                    previous_value = match.group(3).replace(',', '')

                    # Filter out likely header lines or invalid data
                    if len(item_name) < 3 or len(item_name) > 100:
                        continue

                    # Skip if values don't look like numbers
                    try:
                        current_val = float(current_value)
                        previous_val = float(previous_value)
                    except ValueError:
                        continue

                    financial_items.append({
                        'item': item_name,
                        'current_year': current_val,
                        'previous_year': previous_val,
                        'currency': 'GBP'
                    })
                    break

        return financial_items

    def extract_company_info(self) -> Dict[str, str]:
        """
        Extract basic company information from the PDF

        Returns:
            Dictionary with company info (name, number, period, etc.)
        """
        if self.text is None:
            self.extract_text()

        info = {}

        # Extract company name (usually at the top)
        name_pattern = r'([A-Z][A-Z\s&]+(?:LIMITED|LTD|PLC))'
        name_match = re.search(name_pattern, self.text)
        if name_match:
            info['company_name'] = name_match.group(1).strip()

        # Extract company number
        number_pattern = r'Company\s+(?:Number|Registration\s+Number)[:\s]+(\d+)'
        number_match = re.search(number_pattern, self.text, re.IGNORECASE)
        if number_match:
            info['company_number'] = number_match.group(1)

        # Extract period end date
        period_pattern = r'(?:Period\s+End(?:ing)?|Year\s+End(?:ed)?)[:\s]+(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})'
        period_match = re.search(period_pattern, self.text, re.IGNORECASE)
        if period_match:
            info['period_end'] = period_match.group(1)

        return info

    def get_page_count(self) -> int:
        """
        Get the number of pages in the PDF

        Returns:
            Number of pages
        """
        try:
            with pdfplumber.open(BytesIO(self.pdf_content)) as pdf:
                return len(pdf.pages)
        except Exception:
            return 0
