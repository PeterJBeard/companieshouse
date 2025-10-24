"""Financial Data Parser and Formatter"""

import json
import csv
from typing import Dict, List
from tabulate import tabulate
from io import StringIO


class FinancialDataParser:
    """Parse and format financial data"""

    def __init__(self, financial_items: List[Dict], company_info: Dict = None):
        """
        Initialize the parser

        Args:
            financial_items: List of financial line items
            company_info: Company information dictionary
        """
        self.financial_items = financial_items
        self.company_info = company_info or {}

    def to_table(self) -> str:
        """
        Format financial data as a table

        Returns:
            Formatted table string
        """
        if not self.financial_items:
            return "No financial data found"

        headers = ["Item", "Current Year (£)", "Previous Year (£)", "Change (£)", "Change (%)"]
        rows = []

        for item in self.financial_items:
            current = item['current_year']
            previous = item['previous_year']
            change = current - previous
            change_pct = ((change / previous) * 100) if previous != 0 else 0

            rows.append([
                item['item'],
                f"{current:,.2f}",
                f"{previous:,.2f}",
                f"{change:,.2f}",
                f"{change_pct:+.2f}%"
            ])

        table = tabulate(rows, headers=headers, tablefmt="grid")

        # Add company info header if available
        if self.company_info:
            header_lines = []
            if 'company_name' in self.company_info:
                header_lines.append(f"Company: {self.company_info['company_name']}")
            if 'company_number' in self.company_info:
                header_lines.append(f"Number: {self.company_info['company_number']}")
            if 'period_end' in self.company_info:
                header_lines.append(f"Period End: {self.company_info['period_end']}")

            if header_lines:
                header = "\n".join(header_lines)
                table = f"{header}\n\n{table}"

        return table

    def to_json(self, pretty: bool = True) -> str:
        """
        Format financial data as JSON

        Args:
            pretty: Whether to pretty-print the JSON

        Returns:
            JSON string
        """
        data = {
            'company_info': self.company_info,
            'financial_items': self.financial_items,
            'summary': self._calculate_summary()
        }

        if pretty:
            return json.dumps(data, indent=2)
        return json.dumps(data)

    def to_csv(self) -> str:
        """
        Format financial data as CSV

        Returns:
            CSV string
        """
        output = StringIO()
        writer = csv.writer(output)

        # Write headers
        writer.writerow(['Item', 'Current Year (£)', 'Previous Year (£)', 'Change (£)', 'Change (%)'])

        # Write data
        for item in self.financial_items:
            current = item['current_year']
            previous = item['previous_year']
            change = current - previous
            change_pct = ((change / previous) * 100) if previous != 0 else 0

            writer.writerow([
                item['item'],
                current,
                previous,
                change,
                f"{change_pct:.2f}"
            ])

        return output.getvalue()

    def to_dict(self) -> Dict:
        """
        Convert to dictionary format

        Returns:
            Dictionary with all data
        """
        return {
            'company_info': self.company_info,
            'financial_items': self.financial_items,
            'summary': self._calculate_summary()
        }

    def _calculate_summary(self) -> Dict:
        """
        Calculate summary statistics

        Returns:
            Summary dictionary
        """
        if not self.financial_items:
            return {}

        total_current = sum(item['current_year'] for item in self.financial_items)
        total_previous = sum(item['previous_year'] for item in self.financial_items)
        total_change = total_current - total_previous
        total_change_pct = ((total_change / total_previous) * 100) if total_previous != 0 else 0

        return {
            'total_items': len(self.financial_items),
            'total_current_year': total_current,
            'total_previous_year': total_previous,
            'total_change': total_change,
            'total_change_pct': total_change_pct
        }

    def filter_by_keywords(self, keywords: List[str]) -> 'FinancialDataParser':
        """
        Filter financial items by keywords

        Args:
            keywords: List of keywords to search for

        Returns:
            New FinancialDataParser with filtered items
        """
        filtered_items = []

        for item in self.financial_items:
            item_name_lower = item['item'].lower()
            if any(keyword.lower() in item_name_lower for keyword in keywords):
                filtered_items.append(item)

        return FinancialDataParser(filtered_items, self.company_info)

    def get_balance_sheet_items(self) -> 'FinancialDataParser':
        """
        Get items typically found in a balance sheet

        Returns:
            New FinancialDataParser with balance sheet items
        """
        balance_sheet_keywords = [
            'assets', 'liabilities', 'equity', 'cash', 'debtors',
            'creditors', 'stock', 'inventory', 'property', 'equipment',
            'investments', 'reserves', 'capital', 'retained'
        ]

        return self.filter_by_keywords(balance_sheet_keywords)

    def get_profit_loss_items(self) -> 'FinancialDataParser':
        """
        Get items typically found in profit & loss statement

        Returns:
            New FinancialDataParser with P&L items
        """
        pl_keywords = [
            'turnover', 'revenue', 'sales', 'cost', 'gross profit',
            'operating profit', 'expenses', 'depreciation', 'interest',
            'tax', 'profit', 'loss', 'income', 'ebitda'
        ]

        return self.filter_by_keywords(pl_keywords)
