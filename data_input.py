"""
Read and validate the client Excel upload and the proposed-deal form.

Users type rates as percents (6.25 means 6.25%); everything returned from here
is a decimal (0.0625) ready for pricing.py. Every problem is collected and
reported in plain English with its Excel row number; nothing is priced until
the input is clean.
"""

import io
import warnings
import zipfile
from datetime import date

import pandas as pd

from pricing import load_config

CFG = load_config()

# Allowed values come from config.yaml so there is one source of truth
LOAN_TYPES = list(CFG["risk_weights"])
COLLATERAL_TYPES = list(CFG["lgd_by_collateral"])
ACCOUNT_TYPES = list(CFG["deposit_behavior"]) + ["cd"]
RATE_TYPES = ["fixed", "floating"]
RATE_INDEXES = ["SOFR", "Prime"]
AMORTIZATION_TYPES = ["bullet", "level_payment"]
RISK_GRADES = sorted(CFG["pd_by_risk_grade"])

LOAN_COLUMNS = ["loan_id", "loan_type", "balance", "rate_type", "rate", "origination_date", "maturity_date",
                "amortization", "amortization_months", "risk_grade", "collateral_type",
                "prepay_penalty"]
DEPOSIT_COLUMNS = ["account_id", "account_type", "avg_balance", "rate_paid", "ecr_rate",
                   "cd_maturity_date"]
FEE_COLUMNS = ["fee_type", "annual_amount"]

MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_UNZIPPED_BYTES = 50 * 1024 * 1024  # an .xlsx is a zip: stops "zip bombs"
MAX_RATE_PERCENT = 25   # sanity cap: catches typos like 625 instead of 6.25
MAX_MONTHS = 480
EARLIEST_ORIGINATION = date(2001, 8, 1)  # rate history starts here


# ------------------------------------------------------------------
# Field checkers. Each returns the cleaned value, or None after recording
# an error like "Loans row 4: rate is missing."
# ------------------------------------------------------------------

def is_blank(value):
    return value is None or (not isinstance(value, str) and pd.isna(value)) or str(value).strip() == ""


def to_number(value):
    """Accept numbers, or text like '1,250,000' or '$25,000'. Raise ValueError otherwise."""
    if isinstance(value, str):
        value = value.replace(",", "").replace("$", "").replace("%", "").strip()
    return float(value)


def check_number(value, where, field, errors, minimum=0.0, allow_zero=True, maximum=None):
    if is_blank(value):
        errors.append(f"{where}: {field} is missing.")
        return None
    try:
        number = to_number(value)
    except (TypeError, ValueError):
        errors.append(f"{where}: {field} must be a number (got '{value}').")
        return None
    if number < minimum or (number == minimum and not allow_zero):
        errors.append(f"{where}: {field} must be greater than {'or equal to ' if allow_zero else ''}{minimum:g}.")
        return None
    if maximum is not None and number > maximum:
        errors.append(f"{where}: {field} must be no more than {maximum:g} (got {number:g}).")
        return None
    return number


def check_percent(value, where, field, errors, blank_is_zero=False, minimum=0.0):
    """Percent in, decimal out: 6.25 -> 0.0625."""
    if blank_is_zero and is_blank(value):
        return 0.0
    number = check_number(value, where, f"{field} (in percent, e.g. 6.25)", errors,
                          minimum=minimum, maximum=MAX_RATE_PERCENT)
    return None if number is None else number / 100


def check_whole_number(value, where, field, errors, minimum, maximum):
    number = check_number(value, where, field, errors)
    if number is None:
        return None
    if not float(number).is_integer() or not minimum <= number <= maximum:
        errors.append(f"{where}: {field} must be a whole number from {minimum} to {maximum} (got {value}).")
        return None
    return int(number)


def check_choice(value, allowed, where, field, errors):
    """Case-insensitive match against the allowed list; returns the canonical spelling."""
    if is_blank(value):
        errors.append(f"{where}: {field} is missing.")
        return None
    lookup = {str(a).lower(): a for a in allowed}
    key = str(value).strip().lower()
    if key.endswith(".0"):  # Excel turns 4 into 4.0
        key = key[:-2]
    if key not in lookup:
        errors.append(f"{where}: {field} '{value}' is not allowed. Use one of: "
                      f"{', '.join(str(a) for a in allowed)}.")
        return None
    return lookup[key]


def check_yes_no(value, where, field, errors):
    choice = check_choice(value, ["Y", "N"], where, field, errors)
    return None if choice is None else choice == "Y"


def check_future_date(value, today, where, field, errors):
    if is_blank(value):
        errors.append(f"{where}: {field} is missing.")
        return None
    try:
        parsed = pd.to_datetime(value).date()
    except (TypeError, ValueError):
        errors.append(f"{where}: {field} must be a date like 2030-06-30 (got '{value}').")
        return None
    if parsed <= today:
        errors.append(f"{where}: {field} is in the past.")
        return None
    return parsed


def check_origination_date(value, today, where, errors):
    """Must be in the past, and no earlier than the start of the rate history."""
    if is_blank(value):
        errors.append(f"{where}: origination_date is missing.")
        return None
    try:
        parsed = pd.to_datetime(value).date()
    except (TypeError, ValueError):
        errors.append(f"{where}: origination_date must be a date like 2023-06-30 (got '{value}').")
        return None
    if parsed > today:
        errors.append(f"{where}: origination_date is in the future.")
        return None
    if parsed < EARLIEST_ORIGINATION:
        errors.append(f"{where}: origination_date must be on or after "
                      f"{EARLIEST_ORIGINATION.isoformat()} (start of the rate history).")
        return None
    return parsed


def months_between(start, end):
    """Whole months from start to end (at least 1). Day counts are ignored."""
    return max(1, round((end - start).days / (365.25 / 12)))


def check_amortization_months(value, amortization, term_months, where, errors):
    """Only level-payment loans need it, and it can't be shorter than the term."""
    if amortization != "level_payment":
        return 0
    months = check_whole_number(value, where, "amortization_months", errors, 1, MAX_MONTHS)
    if months is not None and term_months is not None and months < term_months:
        errors.append(f"{where}: amortization_months ({months}) is shorter than the "
                      f"term ({term_months} months).")
        return None
    return months


# ------------------------------------------------------------------
# Excel upload
# ------------------------------------------------------------------

def parse_loan_row(row, where, today, errors):
    origination = check_origination_date(row["origination_date"], today, where, errors)
    maturity = check_future_date(row["maturity_date"], today, where, "maturity_date", errors)
    term_months = months_between(today, maturity) if maturity else None
    amortization = check_choice(row["amortization"], AMORTIZATION_TYPES, where, "amortization", errors)
    return {
        "loan_id": "" if is_blank(row["loan_id"]) else str(row["loan_id"]).strip(),
        "loan_type": check_choice(row["loan_type"], LOAN_TYPES, where, "loan_type", errors),
        "balance": check_number(row["balance"], where, "balance", errors, allow_zero=False),
        "rate_type": check_choice(row["rate_type"], RATE_TYPES, where, "rate_type", errors),
        "rate": check_percent(row["rate"], where, "rate", errors),
        "origination_date": origination,
        "elapsed_months": months_between(origination, today) if origination else None,
        "term_months": term_months,  # remaining
        "amortization": amortization,
        "amortization_months": check_amortization_months(  # remaining
            row["amortization_months"], amortization, term_months, where, errors),
        "risk_grade": check_choice(row["risk_grade"], RISK_GRADES, where, "risk_grade", errors),
        "collateral_type": check_choice(row["collateral_type"], COLLATERAL_TYPES, where,
                                        "collateral_type", errors),
        "prepay_penalty": check_yes_no(row["prepay_penalty"], where, "prepay_penalty", errors),
        "upfront_fee": 0.0,  # already earned; not part of year-1 income
    }


def parse_deposit_row(row, where, today, errors):
    account_type = check_choice(row["account_type"], ACCOUNT_TYPES, where, "account_type", errors)
    deposit = {
        "account_id": "" if is_blank(row["account_id"]) else str(row["account_id"]).strip(),
        "account_type": account_type,
        "avg_balance": check_number(row["avg_balance"], where, "avg_balance", errors),
        "rate_paid": check_percent(row["rate_paid"], where, "rate_paid", errors, blank_is_zero=True),
        "ecr_rate": check_percent(row["ecr_rate"], where, "ecr_rate", errors, blank_is_zero=True),
    }
    if account_type == "cd":
        maturity = check_future_date(row["cd_maturity_date"], today, where, "cd_maturity_date", errors)
        deposit["cd_term_months"] = months_between(today, maturity) if maturity else None
    return deposit


def parse_fee_row(row, where, today, errors):
    if is_blank(row["fee_type"]):
        errors.append(f"{where}: fee_type is missing.")
    return {
        "fee_type": str(row["fee_type"]).strip(),
        "annual_amount": check_number(row["annual_amount"], where, "annual_amount", errors),
    }


SHEETS = {
    # sheet name: (required columns, row parser)
    "Loans": (LOAN_COLUMNS, parse_loan_row),
    "Deposits": (DEPOSIT_COLUMNS, parse_deposit_row),
    "Fees": (FEE_COLUMNS, parse_fee_row),
}


def read_client_workbook(file_bytes, filename="upload.xlsx", today=None):
    """
    Return ({"loans": [...], "deposits": [...], "fees": [...]}, errors).
    The file is read in memory only and never written to disk.
    """
    today = today or date.today()
    if not filename.lower().endswith(".xlsx"):
        return None, ["The upload must be an Excel .xlsx file."]
    if len(file_bytes) > MAX_UPLOAD_BYTES:
        return None, ["The upload is larger than 2 MB."]
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as archive:
            if sum(entry.file_size for entry in archive.infolist()) > MAX_UNZIPPED_BYTES:
                return None, ["The workbook is too large once unpacked."]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # openpyxl warns about dropdown validation
            workbook = pd.read_excel(io.BytesIO(file_bytes), sheet_name=None, dtype=object)
    except Exception:
        return None, ["The file could not be read as an Excel workbook."]

    errors = []
    client = {}
    for sheet, (columns, parse_row) in SHEETS.items():
        rows = []
        client[sheet.lower()] = rows
        if sheet not in workbook:
            errors.append(f"The workbook is missing the '{sheet}' sheet.")
            continue
        df = workbook[sheet]
        df.columns = [str(c).strip() for c in df.columns]
        df = df.dropna(how="all")  # ignore blank rows
        if df.empty:
            continue  # an empty sheet is fine (e.g. a brand-new client)
        missing = [c for c in columns if c not in df.columns]
        if missing:
            errors.append(f"{sheet} sheet: missing column(s) {', '.join(missing)}.")
            continue
        for index, row in df.iterrows():
            excel_row = index + 2  # row 1 is the header; pandas counts from 0
            rows.append(parse_row(row, f"{sheet} row {excel_row}", today, errors))
    return client, errors


# ------------------------------------------------------------------
# Proposed deal (web form)
# ------------------------------------------------------------------

def parse_deal_form(form):
    """Validate the deal form (a dict of strings). Return (deal, errors)."""
    errors = []
    where = "Deal"
    rate_type = check_choice(form.get("rate_type"), RATE_TYPES, where, "rate type", errors)
    amortization = check_choice(form.get("amortization"), AMORTIZATION_TYPES, where, "amortization", errors)
    term_months = check_whole_number(form.get("term_months"), where, "term (months)", errors, 1, MAX_MONTHS)

    deal = {
        "loan_type": check_choice(form.get("loan_type"), LOAN_TYPES, where, "loan type", errors),
        "balance": check_number(form.get("amount"), where, "amount", errors, allow_zero=False),
        "rate_type": rate_type,
        "fixed_rate": 0.0,
        "float_margin": 0.0,
        "rate_index": "SOFR",
        "rate_floor": 0.0,
        "term_months": term_months,
        "amortization": amortization,
        "amortization_months": check_amortization_months(
            form.get("amortization_months"), amortization, term_months, where, errors),
        "risk_grade": check_choice(form.get("risk_grade"), RISK_GRADES, where, "risk grade", errors),
        "collateral_type": check_choice(form.get("collateral_type"), COLLATERAL_TYPES, where,
                                        "collateral type", errors),
        "prepay_penalty": check_yes_no(form.get("prepay_penalty"), where, "prepay penalty", errors),
        "upfront_fee": check_number(form.get("upfront_fee") or 0, where, "upfront fee", errors),
    }
    if rate_type == "fixed":
        deal["fixed_rate"] = check_percent(form.get("fixed_rate"), where, "fixed rate", errors)
    elif rate_type == "floating":
        deal["rate_index"] = check_choice(form.get("rate_index"), RATE_INDEXES, where, "rate index", errors)
        deal["float_margin"] = check_percent(form.get("float_margin"), where, "margin over index", errors,
                                             minimum=-MAX_RATE_PERCENT)
        deal["rate_floor"] = check_percent(form.get("rate_floor"), where, "rate floor", errors, blank_is_zero=True)
    return deal, errors
