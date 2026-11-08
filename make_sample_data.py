"""
Generate static/template.xlsx (blank template with dropdowns) and
static/sample_client.xlsx (a fictional client) for the app.

Run:  python make_sample_data.py

Dates are set relative to today so the sample client never has a maturity in
the past. All names and numbers are made up.
"""

from datetime import date
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

from data_input import (
    ACCOUNT_TYPES, AMORTIZATION_TYPES, COLLATERAL_TYPES, DEPOSIT_COLUMNS, FEE_COLUMNS,
    LOAN_COLUMNS, LOAN_TYPES, RATE_TYPES, RISK_GRADES,
)

STATIC = Path(__file__).parent / "static"
LAST_ROW = 500  # dropdowns cover this many rows


def months_from_today(months):
    """Same day-of-month, `months` ahead (day capped at 28 to stay valid)."""
    today = date.today()
    total = today.month - 1 + months
    return date(today.year + total // 12, total % 12 + 1, min(today.day, 28))


# Dropdowns: sheet -> {column name: allowed values}
DROPDOWNS = {
    "Loans": {
        "loan_type": LOAN_TYPES,
        "rate_type": RATE_TYPES,
        "amortization": AMORTIZATION_TYPES,
        "risk_grade": RISK_GRADES,
        "collateral_type": COLLATERAL_TYPES,
        "prepay_penalty": ["Y", "N"],
    },
    "Deposits": {"account_type": ACCOUNT_TYPES},
    "Fees": {},
}
COLUMNS = {"Loans": LOAN_COLUMNS, "Deposits": DEPOSIT_COLUMNS, "Fees": FEE_COLUMNS}


def write_workbook(path, rows_by_sheet):
    wb = Workbook()
    wb.remove(wb.active)
    header_fill = PatternFill("solid", fgColor="DDE5EE")
    for sheet, rows in rows_by_sheet.items():
        ws = wb.create_sheet(sheet)
        columns = COLUMNS[sheet]
        ws.append(columns)
        for cell in ws[1]:
            cell.font = Font(bold=True)
            cell.fill = header_fill
        for row in rows:
            ws.append([row.get(c) for c in columns])
        for i, name in enumerate(columns, start=1):
            letter = ws.cell(row=1, column=i).column_letter
            ws.column_dimensions[letter].width = max(14, len(name) + 4)
            if name in DROPDOWNS[sheet]:
                values = ",".join(str(v) for v in DROPDOWNS[sheet][name])
                dv = DataValidation(type="list", formula1=f'"{values}"', allow_blank=True)
                ws.add_data_validation(dv)
                dv.add(f"{letter}2:{letter}{LAST_ROW}")
            if name.endswith("date"):
                for r in range(2, ws.max_row + 1):
                    ws.cell(row=r, column=i).number_format = "yyyy-mm-dd"
        ws.freeze_panes = "A2"
    wb.save(path)


# ------------------------------------------------------------------
# template.xlsx: one example row per sheet
# ------------------------------------------------------------------

TEMPLATE = {
    "Loans": [{
        "loan_id": "L-001", "loan_type": "CRE", "balance": 2500000, "rate_type": "fixed",
        "rate": 6.25, "origination_date": months_from_today(-12),
        "maturity_date": months_from_today(48), "amortization": "level_payment",
        "amortization_months": 300, "risk_grade": 4, "collateral_type": "real_estate",
        "prepay_penalty": "Y",
    }],
    "Deposits": [{
        "account_id": "D-001", "account_type": "dda", "avg_balance": 750000,
        "rate_paid": 0, "ecr_rate": 0, "cd_maturity_date": None,
    }],
    "Fees": [{"fee_type": "Treasury management - ACH", "annual_amount": 12000}],
}

# ------------------------------------------------------------------
# sample_client.xlsx: ABC LLC (fictional)
# ------------------------------------------------------------------

SAMPLE = {
    "Loans": [
        {"loan_id": "HMF-001", "loan_type": "CRE", "balance": 3850000, "rate_type": "fixed",
         "rate": 6.25, "origination_date": months_from_today(-40),  # 7-yr loan, 40 months in
         "maturity_date": months_from_today(44), "amortization": "level_payment",
         "amortization_months": 260, "risk_grade": 4, "collateral_type": "real_estate",
         "prepay_penalty": "Y"},
        {"loan_id": "HMF-002", "loan_type": "CI", "balance": 1200000, "rate_type": "floating",
         "rate": 7.05, "origination_date": months_from_today(-30),  # revolver
         "maturity_date": months_from_today(18), "amortization": "bullet",
         "amortization_months": None, "risk_grade": 4, "collateral_type": "receivables_inventory",
         "prepay_penalty": "N"},
        {"loan_id": "HMF-003", "loan_type": "CI", "balance": 640000, "rate_type": "fixed",
         "rate": 6.85, "origination_date": months_from_today(-26),  # 5-yr equipment loan
         "maturity_date": months_from_today(34), "amortization": "level_payment",
         "amortization_months": 34, "risk_grade": 5, "collateral_type": "equipment",
         "prepay_penalty": "N"},
    ],
    "Deposits": [
        {"account_id": "HMF-DDA-01", "account_type": "dda", "avg_balance": 450000,
         "rate_paid": 0, "ecr_rate": 0},
        {"account_id": "HMF-DDA-02", "account_type": "dda", "avg_balance": 900000,
         "rate_paid": 0, "ecr_rate": 1.25},
        {"account_id": "HMF-HYS-01", "account_type": "high_yield_savings", "avg_balance": 250000,
         "rate_paid": 3.40, "ecr_rate": 0},
        {"account_id": "HMF-MM-01", "account_type": "mmda", "avg_balance": 1200000,
         "rate_paid": 3.10, "ecr_rate": 0},
        {"account_id": "HMF-CD-01", "account_type": "cd", "avg_balance": 800000,
         "rate_paid": 3.85, "ecr_rate": 0, "cd_maturity_date": months_from_today(14)},
    ],
    "Fees": [
        {"fee_type": "Treasury management - ACH and wires", "annual_amount": 12500},
        {"fee_type": "Remote deposit capture and lockbox", "annual_amount": 6200},
    ],
}


if __name__ == "__main__":
    STATIC.mkdir(exist_ok=True)
    write_workbook(STATIC / "template.xlsx", TEMPLATE)
    write_workbook(STATIC / "sample_client.xlsx", SAMPLE)
    print(f"Wrote {STATIC / 'template.xlsx'} and {STATIC / 'sample_client.xlsx'}")
