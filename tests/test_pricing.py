"""
Tests for the loan pricing model. No network calls: FRED is replaced by fakes
and the pricing tests use a fixed, made-up curve.
"""

import json
from datetime import date

import pytest

import market_data
from market_data import (
    MarketDataError,
    build_funding_curve,
    curve_for_month,
    curve_rate,
    get_market_data,
    get_rate_history,
    interpolate,
)

TODAY = date(2026, 1, 15)

# A fixed fake market: Treasury curve from 4.00% to 5.00%, SOFR 4.30%
FAKE_MARKET = {
    "as_of": "2026-01-14",
    "treasury": [
        {"series": "DGS1MO", "tenor": 1 / 12, "rate": 0.0400, "date": "2026-01-14"},
        {"series": "DGS1", "tenor": 1, "rate": 0.0410, "date": "2026-01-14"},
        {"series": "DGS2", "tenor": 2, "rate": 0.0420, "date": "2026-01-14"},
        {"series": "DGS5", "tenor": 5, "rate": 0.0440, "date": "2026-01-14"},
        {"series": "DGS10", "tenor": 10, "rate": 0.0470, "date": "2026-01-14"},
        {"series": "DGS30", "tenor": 30, "rate": 0.0500, "date": "2026-01-14"},
    ],
    "sofr": 0.0430,
    "sofr_date": "2026-01-14",
    "prime": 0.0700,
    "prime_date": "2026-01-14",
}


# Fake monthly history: a flat 2.00% curve in mid-2023, 3.00% in Jan 2024
# (with no 30-year that month), and the 1-month only in Feb 2024
FAKE_HISTORY = {
    "as_of": "2024-02",
    "months": {
        "2023-06": {s: 0.0200 for s in market_data.TREASURY_SERIES},
        "2024-01": {s: 0.0300 for s in market_data.TREASURY_SERIES if s != "DGS30"},
        "2024-02": {"DGS1MO": 0.0310},
    },
}


@pytest.fixture
def curve():
    # Funding curve = Treasury + 25 bp
    return build_funding_curve(FAKE_MARKET, 25)


# ------------------------------------------------------------------
# Milestone 1: market data
# ------------------------------------------------------------------

def test_interpolation_between_points():
    points = [(1, 0.04), (3, 0.06)]
    assert interpolate(points, 2) == pytest.approx(0.05)
    assert interpolate(points, 1.5) == pytest.approx(0.045)


def test_interpolation_is_flat_beyond_the_ends():
    points = [(1, 0.04), (3, 0.06)]
    assert interpolate(points, 0.1) == 0.04
    assert interpolate(points, 50) == 0.06


def test_funding_curve_adds_spread(curve):
    assert curve_rate(curve, 1) == pytest.approx(0.0410 + 0.0025)
    assert curve_rate(curve, 3.5) == pytest.approx(0.0430 + 0.0025)  # halfway 2y-5y
    assert curve_rate(curve, 0.01) == pytest.approx(0.0400 + 0.0025)  # flat short end
    assert curve_rate(curve, 40) == pytest.approx(0.0500 + 0.0025)  # flat long end


def test_fetch_latest_skips_missing_values(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"observations": [
                {"date": "2026-01-12", "value": "4.10"},
                {"date": "2026-01-13", "value": "4.15"},
                {"date": "2026-01-14", "value": "."},  # holiday / missing
            ]}

    monkeypatch.setattr(market_data.requests, "get", lambda *a, **k: FakeResponse())
    rate, obs_date = market_data.fetch_latest("DGS1", "key", TODAY)
    assert rate == pytest.approx(0.0415)
    assert obs_date == "2026-01-13"


def test_cache_written_then_reused(tmp_path):
    calls = []

    def fake_fetch(api_key, today):
        calls.append(today)
        return dict(FAKE_MARKET)

    first = get_market_data("key", TODAY, tmp_path, fake_fetch)
    second = get_market_data("key", TODAY, tmp_path, fake_fetch)
    assert (tmp_path / "market_2026-01-15.json").exists()
    assert len(calls) == 1  # second call came from the cache
    assert first["warning"] is None and second["warning"] is None
    assert second["sofr"] == FAKE_MARKET["sofr"]


def test_falls_back_to_latest_cache_with_warning(tmp_path):
    (tmp_path / "market_2026-01-09.json").write_text(json.dumps({**FAKE_MARKET, "as_of": "2026-01-09"}))
    (tmp_path / "market_2026-01-12.json").write_text(json.dumps({**FAKE_MARKET, "as_of": "2026-01-12"}))

    def failing_fetch(api_key, today):
        raise ConnectionError("FRED down")

    market = get_market_data("key", TODAY, tmp_path, failing_fetch)
    assert market["as_of"] == "2026-01-12"
    assert "cached market data as of 2026-01-12" in market["warning"]


def test_no_cache_and_no_fred_is_a_clear_error(tmp_path):
    with pytest.raises(MarketDataError, match="No market data available"):
        get_market_data(None, TODAY, tmp_path)


def test_curve_for_month_uses_that_month_or_the_latest_before_it():
    assert curve_for_month(FAKE_HISTORY, "2023-06")["treasury"][0]["rate"] == 0.0200
    assert curve_for_month(FAKE_HISTORY, "2023-11")["as_of"] == "2023-06"  # no Nov data
    jan = curve_for_month(FAKE_HISTORY, "2024-01")
    assert "DGS30" not in [p["series"] for p in jan["treasury"]]  # missing series left out
    assert curve_for_month(FAKE_HISTORY, "2019-01")["as_of"] == "2023-06"  # before history


def test_history_and_daily_caches_do_not_mix(tmp_path):
    (tmp_path / "fred_history_2025-12.json").write_text(json.dumps(FAKE_HISTORY))
    with pytest.raises(MarketDataError):  # a history file is not a daily curve
        get_market_data(None, TODAY, tmp_path)
    history = get_rate_history(None, TODAY, tmp_path)
    assert history["months"]["2023-06"]["DGS1"] == 0.02
    assert "cached rate history" in history["warning"]


# ------------------------------------------------------------------
# Milestone 2: pricing math
# ------------------------------------------------------------------

from pricing import (  # noqa: E402
    as_originated,
    deposit_ftp_credit,
    existing_loan_contribution,
    floor_credit,
    load_config,
    loan_components,
    loan_contribution,
    option_cost,
    price_deal,
    principal_schedule,
    strip_ftp,
)

CFG = load_config()


def make_loan(**overrides):
    loan = {
        "loan_type": "CRE", "balance": 5_000_000, "rate_type": "fixed", "rate": 0.065,
        "term_months": 60, "amortization": "level_payment", "amortization_months": 300,
        "risk_grade": 4, "collateral_type": "real_estate", "prepay_penalty": False,
        "upfront_fee": 25_000,
    }
    loan.update(overrides)
    return loan


def make_deal(**overrides):
    deal = make_loan(**overrides)
    deal.setdefault("fixed_rate", deal["rate"])
    deal.setdefault("float_margin", 0.0)
    deal.setdefault("rate_index", "SOFR")
    deal.setdefault("rate_floor", 0.0)
    return deal


def test_level_payment_principal_sums_to_balance():
    schedule = principal_schedule(1_000_000, 60, 0.07, "level_payment", 240)
    assert len(schedule) == 60
    assert sum(p for _, p in schedule) == pytest.approx(1_000_000)
    assert schedule[-1][1] > schedule[0][1] * 10  # balloon at maturity


def test_fully_amortizing_has_no_balloon():
    schedule = principal_schedule(1_000_000, 120, 0.06, "level_payment", 120)
    assert sum(p for _, p in schedule) == pytest.approx(1_000_000)
    assert schedule[-1][1] == pytest.approx(schedule[-2][1] * (1 + 0.06 / 12), rel=1e-6)


def test_bullet_strip_ftp_equals_curve_at_maturity(curve):
    schedule = principal_schedule(2_000_000, 42, 0.07, "bullet", 0)
    assert strip_ftp(schedule, curve) == pytest.approx(curve_rate(curve, 3.5))


def test_amortizing_ftp_is_below_maturity_rate_on_upward_curve(curve):
    schedule = principal_schedule(2_000_000, 120, 0.07, "level_payment", 120)
    assert curve_rate(curve, 1) < strip_ftp(schedule, curve) < curve_rate(curve, 10)


def test_floating_ftp_is_sofr_plus_funding_spread(curve):
    loan = make_loan(rate_type="floating", rate=0.068)
    comp = loan_components(loan, curve, FAKE_MARKET["sofr"], CFG)
    assert comp["ftp"] == pytest.approx(0.0430 + 0.0025)
    assert comp["option"] == 0.0


def test_option_cost_only_on_fixed_without_penalty():
    assert option_cost(make_loan(), CFG) > 0
    assert option_cost(make_loan(prepay_penalty=True), CFG) == 0.0
    assert option_cost(make_loan(rate_type="floating"), CFG) == 0.0


def test_option_cost_is_zero_without_prepayments_or_volatility():
    assert option_cost(make_loan(), {**CFG, "cpr_by_collateral": {"real_estate": 0.0}}) == 0.0
    assert option_cost(make_loan(), {**CFG, "rate_volatility_bps": 0}) == 0.0


def test_option_cost_rises_with_cpr_and_term():
    fast = {**CFG, "cpr_by_collateral": {"real_estate": 0.20}}
    slow = {**CFG, "cpr_by_collateral": {"real_estate": 0.05}}
    assert option_cost(make_loan(), fast) > option_cost(make_loan(), slow)
    assert option_cost(make_loan(term_months=120), CFG) > option_cost(make_loan(term_months=36), CFG)
    # A plausible size: a 5-year CRE loan at 8% CPR and 100 bp vol costs ~10 bp a year
    assert 0.0005 < option_cost(make_loan(), CFG) < 0.0020


@pytest.mark.parametrize("overrides", [
    {},
    {"amortization": "bullet", "upfront_fee": 0},
    {"rate_type": "floating", "loan_type": "Construction", "risk_grade": 6},
    {"loan_type": "CI", "collateral_type": "unsecured", "term_months": 18, "prepay_penalty": True},
])
def test_roe_at_standalone_required_rate_equals_hurdle(curve, overrides):
    loan = make_loan(**overrides)
    comp = loan_components(loan, curve, FAKE_MARKET["sofr"], CFG)
    result = loan_contribution(loan, comp, CFG, rate=comp["required_rate"])
    assert result["roe"] == pytest.approx(CFG["hurdle_roe"])


def test_total_roe_at_relationship_rate_equals_hurdle():
    loans = [make_loan(balance=3_000_000, rate=0.058, upfront_fee=0, term_months=36),
             make_loan(rate_type="floating", rate=0.071, loan_type="CI", upfront_fee=0)]
    deposits = [
        {"account_type": "dda", "avg_balance": 2_500_000, "rate_paid": 0.0, "ecr_rate": 0.0},
        {"account_type": "cd", "avg_balance": 1_000_000, "rate_paid": 0.039, "ecr_rate": 0.0,
         "cd_term_months": 9},
    ]
    fees = [{"fee_type": "ACH", "annual_amount": 18_000}]
    for loan in loans:
        loan.update(origination_date=date(2024, 1, 10), elapsed_months=24)
    result = price_deal(make_deal(), loans, deposits, fees, FAKE_MARKET, CFG, FAKE_HISTORY)

    # Re-price the deal at the relationship rate, keeping its components
    # (and so its amortization schedule and FTP) fixed at the offered rate
    comp = result["components"]
    deal_row = loan_contribution(result["deal"], comp, CFG, rate=result["relationship_rate"])
    others = result["relationship_table"][:3]
    total_after_tax = (sum(r["pretax"] for r in others) + deal_row["pretax"]) * (1 - CFG["tax_rate"])
    total_capital = sum(r["capital"] for r in others) + deal_row["capital"]
    assert total_after_tax / total_capital == pytest.approx(CFG["hurdle_roe"])

    # A profitable relationship lowers the rate the deal needs
    assert result["relationship_rate"] < result["standalone_rate"]


def test_existing_loan_ftp_is_locked_at_origination_curve(curve):
    # Bullet loan booked June 2023 (flat 2.00% curve), 30 months in, 30 to go
    loan = make_loan(amortization="bullet", term_months=30, upfront_fee=0,
                     origination_date=date(2023, 6, 20), elapsed_months=30)
    row = existing_loan_contribution(loan, curve, FAKE_MARKET["sofr"], FAKE_HISTORY, CFG)
    assert row["ftp"] == pytest.approx(0.0200 + 0.0025)
    assert row["curve_month"] == "2023-06"
    # Without history it falls back to today's curve over the remaining term
    fallback = existing_loan_contribution(loan, curve, FAKE_MARKET["sofr"], None, CFG)
    assert fallback["ftp"] == pytest.approx(curve_rate(curve, 2.5))


def test_as_originated_restores_original_term_and_amortization():
    loan = make_loan(term_months=44, amortization_months=260, elapsed_months=40)
    original = as_originated(loan)
    assert original["term_months"] == 84 and original["amortization_months"] == 300


def test_deposit_credit_blends_stable_and_volatile(curve):
    dda = {"account_type": "dda", "avg_balance": 1e6, "rate_paid": 0.0, "ecr_rate": 0.0}
    behavior = CFG["deposit_behavior"]["dda"]
    expected = (behavior["stable_share"] * curve_rate(curve, behavior["stable_life_years"])
                + (1 - behavior["stable_share"]) * curve_rate(curve, CFG["volatile_life_years"]))
    assert deposit_ftp_credit(dda, curve, CFG) == (pytest.approx(expected), behavior["stable_share"])
    cd = {"account_type": "cd", "cd_term_months": 24}
    assert deposit_ftp_credit(cd, curve, CFG) == (pytest.approx(curve_rate(curve, 2)), 1.0)


def test_new_client_relationship_rate_equals_standalone():
    result = price_deal(make_deal(), [], [], [], FAKE_MARKET, CFG)
    assert result["relationship_rate"] == pytest.approx(result["standalone_rate"])
    assert result["relationship_roe"] == pytest.approx(result["standalone_roe"])


def test_floating_deal_offered_rate_is_sofr_plus_margin():
    deal = make_deal(rate_type="floating", float_margin=0.0250)
    result = price_deal(deal, [], [], [], FAKE_MARKET, CFG)
    assert result["offered_rate"] == pytest.approx(0.0430 + 0.0250)


def test_prime_deal_is_quoted_off_prime_but_funded_at_sofr():
    deal = make_deal(rate_type="floating", rate_index="Prime", float_margin=-0.0025)
    result = price_deal(deal, [], [], [], FAKE_MARKET, CFG)
    assert result["offered_rate"] == pytest.approx(0.0700 - 0.0025)
    assert result["components"]["ftp"] == pytest.approx(0.0430 + 0.0025)


def floating_with_floor(rate_floor):
    deal = make_deal(rate_type="floating", float_margin=0.0250, rate_floor=rate_floor)
    return price_deal(deal, [], [], [], FAKE_MARKET, CFG)


def test_binding_floor_sets_the_offered_rate():
    assert floating_with_floor(0.0750)["offered_rate"] == 0.0750
    assert floating_with_floor(0.0600)["offered_rate"] == pytest.approx(0.0680)


def test_floor_reduces_the_required_rate_and_still_earns_the_hurdle():
    without, floored = floating_with_floor(0.0), floating_with_floor(0.0650)
    credit = floored["components"]["floor_credit"]
    assert without["components"]["floor_credit"] == 0.0
    assert credit > 0
    assert floored["standalone_rate"] == pytest.approx(without["standalone_rate"] - credit)
    row = loan_contribution(floored["deal"], floored["components"], CFG, rate=floored["standalone_rate"])
    assert row["roe"] == pytest.approx(CFG["hurdle_roe"])


def test_floor_credit_is_time_value_only():
    at_the_money = floating_with_floor(0.0680)["components"]["floor_credit"]
    far_below = floating_with_floor(0.0100)["components"]["floor_credit"]
    far_above = floating_with_floor(0.1500)["components"]["floor_credit"]
    assert far_below == pytest.approx(0, abs=1e-4)
    assert far_above == pytest.approx(0, abs=1e-4)
    assert at_the_money > 0.0020


def test_floor_credit_is_zero_for_fixed_loans_or_zero_volatility():
    assert floor_credit(make_loan(rate_floor=0.06, float_margin=0.0, index_rate=0.05), CFG) == 0.0
    floating = make_loan(rate_type="floating", rate_floor=0.06, float_margin=0.0, index_rate=0.05)
    assert floor_credit(floating, {**CFG, "rate_volatility_bps": 0}) == 0.0


# ------------------------------------------------------------------
# Milestone 3: input validation
# ------------------------------------------------------------------

import io  # noqa: E402

from openpyxl import Workbook  # noqa: E402

from data_input import (  # noqa: E402
    DEPOSIT_COLUMNS,
    FEE_COLUMNS,
    LOAN_COLUMNS,
    parse_deal_form,
    read_client_workbook,
)

GOOD_LOAN = ["L-1", "CRE", 2000000, "fixed", 6.25, date(2023, 6, 30), date(2029, 6, 30),
             "level_payment", 300, 4, "real_estate", "Y"]


def workbook_bytes(loans=(), deposits=(), fees=(), loan_columns=LOAN_COLUMNS):
    wb = Workbook()
    wb.remove(wb.active)
    for name, columns, rows in [("Loans", loan_columns, loans),
                                ("Deposits", DEPOSIT_COLUMNS, deposits),
                                ("Fees", FEE_COLUMNS, fees)]:
        ws = wb.create_sheet(name)
        ws.append(columns)
        for row in rows:
            ws.append(list(row))
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def test_clean_workbook_converts_percents_to_decimals():
    data = workbook_bytes(
        loans=[GOOD_LOAN],
        deposits=[["D-1", "cd", 500000, 3.9, 0, date(2026, 10, 15)]],
        fees=[["ACH", 12000]],
    )
    client, errors = read_client_workbook(data, today=TODAY)
    assert errors == []
    assert client["loans"][0]["rate"] == pytest.approx(0.0625)
    assert client["loans"][0]["term_months"] == 41  # Jan 2026 -> Jun 2029, rounded
    assert client["loans"][0]["elapsed_months"] == 31  # Jun 2023 -> Jan 2026
    assert client["deposits"][0]["cd_term_months"] == 9


def test_validation_reports_correct_excel_row_numbers():
    bad = list(GOOD_LOAN)
    bad[6] = date(2025, 3, 31)  # maturity in the past
    bad[1] = "Mortgage"         # not an allowed loan type
    data = workbook_bytes(loans=[GOOD_LOAN, GOOD_LOAN, bad])  # bad loan is Excel row 4
    _, errors = read_client_workbook(data, today=TODAY)
    assert "Loans row 4: maturity_date is in the past." in errors
    assert any(e.startswith("Loans row 4: loan_type 'Mortgage' is not allowed") for e in errors)
    assert len(errors) == 2  # rows 2 and 3 are clean


def test_validation_checks_origination_date():
    future, ancient = list(GOOD_LOAN), list(GOOD_LOAN)
    future[5] = date(2026, 6, 1)
    ancient[5] = date(1999, 1, 1)
    _, errors = read_client_workbook(workbook_bytes(loans=[future, ancient]), today=TODAY)
    assert errors == [
        "Loans row 2: origination_date is in the future.",
        "Loans row 3: origination_date must be on or after 2001-08-01 (start of the rate history).",
    ]


def test_validation_catches_missing_columns_and_bad_types():
    data = workbook_bytes(
        loans=[GOOD_LOAN[:-1]], loan_columns=LOAN_COLUMNS[:-1],  # no prepay_penalty
        deposits=[["D-1", "savings", "lots", 0, 0, None]],
    )
    _, errors = read_client_workbook(data, today=TODAY)
    assert "Loans sheet: missing column(s) prepay_penalty." in errors
    assert "Deposits row 2: avg_balance must be a number (got 'lots')." in errors


def test_empty_sheets_are_a_valid_new_client():
    client, errors = read_client_workbook(workbook_bytes(), today=TODAY)
    assert errors == []
    assert client == {"loans": [], "deposits": [], "fees": []}


def test_rejects_non_xlsx_upload():
    _, errors = read_client_workbook(b"a,b,c", filename="client.csv")
    assert errors == ["The upload must be an Excel .xlsx file."]


DEAL_FORM = {
    "loan_type": "CRE", "amount": "4,000,000", "term_months": "60", "rate_type": "fixed",
    "fixed_rate": "6.50", "float_margin": "", "amortization": "level_payment",
    "amortization_months": "300", "risk_grade": "4", "collateral_type": "real_estate",
    "prepay_penalty": "N", "upfront_fee": "20000",
}


def test_deal_form_parses_cleanly():
    deal, errors = parse_deal_form(DEAL_FORM)
    assert errors == []
    assert deal["balance"] == 4_000_000
    assert deal["fixed_rate"] == pytest.approx(0.065)
    assert deal["risk_grade"] == 4 and deal["prepay_penalty"] is False


def test_deal_form_collects_all_errors():
    form = {**DEAL_FORM, "amount": "-5", "fixed_rate": "", "amortization_months": "24"}
    _, errors = parse_deal_form(form)
    assert len(errors) == 3
    assert "Deal: amortization_months (24) is shorter than the term (60 months)." in errors


def test_deal_form_reads_prime_negative_margin_and_floor():
    form = {**DEAL_FORM, "rate_type": "floating", "rate_index": "prime",
            "float_margin": "-0.25", "rate_floor": "5.5"}
    deal, errors = parse_deal_form(form)
    assert errors == []
    assert deal["rate_index"] == "Prime"
    assert deal["float_margin"] == pytest.approx(-0.0025)
    assert deal["rate_floor"] == pytest.approx(0.055)


def test_deal_form_defaults_to_no_floor_and_rejects_unknown_index():
    floating = {**DEAL_FORM, "rate_type": "floating", "rate_index": "SOFR", "float_margin": "2.5"}
    assert parse_deal_form(floating)[0]["rate_floor"] == 0.0
    _, errors = parse_deal_form({**floating, "rate_index": "LIBOR"})
    assert any("rate index 'LIBOR' is not allowed" in e for e in errors)


# ------------------------------------------------------------------
# Milestone 4: the web app end to end (fake market data, no network)
# ------------------------------------------------------------------

TEST_PASSWORD = "correct-horse-battery"


@pytest.fixture
def anonymous(monkeypatch):
    """A browser with no session, on a server whose password is TEST_PASSWORD."""
    import app
    import auth
    from fastapi.testclient import TestClient

    monkeypatch.setenv("APP_PASSWORD", TEST_PASSWORD)
    auth._sessions.clear()
    auth._failures.clear()
    return TestClient(app.app, base_url="http://127.0.0.1", follow_redirects=False)


@pytest.fixture
def web(anonymous):
    """The same browser, logged in."""
    assert anonymous.post("/login", data={"password": TEST_PASSWORD}).status_code == 303
    return anonymous


def test_app_prices_sample_client_end_to_end(monkeypatch, web):
    import app

    monkeypatch.setattr(app, "get_market_data", lambda key: {**FAKE_MARKET, "warning": None})
    monkeypatch.setattr(app, "get_rate_history", lambda key: {**FAKE_HISTORY, "warning": None})
    client = web
    assert client.get("/").status_code == 200

    response = client.post("/price", data={**app.DEFAULT_DEAL, "use_sample": "on"})
    assert response.status_code == 200
    assert "Relationship-adjusted required rate" in response.text
    assert "ABC LLC" in response.text
    assert "curve</span>" in response.text  # existing loans show their origination curve
    assert "0.25% PD &times; 30.00% LGD" in response.text  # the build-up shows its inputs
    assert "$20,000 &divide; 5 yrs &divide; $4,000,000" in response.text

    bad = client.post("/price", data={**app.DEFAULT_DEAL, "risk_grade": "9"})
    assert bad.status_code == 400
    assert "Deal: risk grade &#39;9&#39; is not allowed" in bad.text


def test_app_shows_prime_deal_with_floor(monkeypatch, web):
    import app

    monkeypatch.setattr(app, "get_market_data", lambda key: {**FAKE_MARKET, "warning": None})
    form = {**app.DEFAULT_DEAL, "rate_type": "floating", "rate_index": "Prime",
            "float_margin": "-0.25", "rate_floor": "7.25"}
    response = web.post("/price", data=form)
    assert response.status_code == 200
    assert "At floor: Prime 7.00% -25 bp" in response.text
    assert "Rate floor credit" in response.text
    assert "Prime 7.00% as of 2026-01-14" in response.text


# ------------------------------------------------------------------
# Security: client data stays in memory and out of every cache
# ------------------------------------------------------------------

import tempfile  # noqa: E402
import zipfile  # noqa: E402

import openpyxl  # noqa: E402

from data_input import MAX_UNZIPPED_BYTES  # noqa: E402


@pytest.fixture
def spooled_to_disk(monkeypatch):
    """Records every upload that rolls over from memory to a temp file."""
    rolled = []
    real_rollover = tempfile.SpooledTemporaryFile.rollover

    def spy(self):
        rolled.append(self)
        return real_rollover(self)

    monkeypatch.setattr(tempfile.SpooledTemporaryFile, "rollover", spy)
    return rolled


def test_chunked_upload_over_the_limit_never_reaches_disk(spooled_to_disk, web):
    # A chunked request has no Content-Length, so only counting the bytes can stop it
    boundary = "TESTBOUNDARY"
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="client_file"; '
            'filename="big.xlsx"\r\n\r\n').encode() + b"A" * (6 * 1024 * 1024) + f"\r\n--{boundary}--\r\n".encode()
    response = web.post(
        "/price", content=iter([body]),
        headers={"content-type": f"multipart/form-data; boundary={boundary}"})
    assert response.status_code == 413
    assert spooled_to_disk == []


def test_largest_allowed_upload_stays_in_memory(spooled_to_disk, web):
    import app

    response = web.post(
        "/price", data=app.DEFAULT_DEAL,
        files={"client_file": ("client.xlsx", b"A" * (2 * 1024 * 1024))})
    assert "could not be read as an Excel workbook" in response.text
    assert spooled_to_disk == []


def test_client_data_never_reaches_the_market_data_cache(monkeypatch, tmp_path, web):
    import app

    monkeypatch.setattr(app, "get_market_data", lambda key: market_data.get_market_data(
        "key", cache_dir=tmp_path, fetch=lambda k, t: dict(FAKE_MARKET)))
    monkeypatch.setattr(app, "get_rate_history", lambda key: market_data.get_rate_history(
        "key", cache_dir=tmp_path, fetch=lambda k, t: dict(FAKE_HISTORY)))

    workbook = workbook_bytes(loans=[["SECRET-LOAN-ID", *GOOD_LOAN[1:]]])
    response = web.post(
        "/price", data=app.DEFAULT_DEAL, files={"client_file": ("Acme Holdings.xlsx", workbook)})
    assert response.status_code == 200

    cache_files = list(tmp_path.iterdir())
    assert len(cache_files) == 2  # the daily curve and the history were both written
    for cache_file in cache_files:
        contents = cache_file.read_text()
        assert "SECRET-LOAN-ID" not in contents and "Acme" not in contents


def test_results_page_is_not_cached_and_loads_nothing_from_other_sites(monkeypatch, web):
    import app

    monkeypatch.setattr(app, "get_market_data", lambda key: {**FAKE_MARKET, "warning": None})
    monkeypatch.setattr(app, "get_rate_history", lambda key: {**FAKE_HISTORY, "warning": None})
    client = web
    response = client.post("/price", data={**app.DEFAULT_DEAL, "use_sample": "on"})

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert "https://" not in response.text  # no third-party scripts or links
    assert "<script>" not in response.text  # the policy would block inline scripts
    assert client.get("/docs").status_code == 404


def test_rejects_workbook_that_unpacks_too_large():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/worksheets/sheet1.xml", b"0" * (MAX_UNZIPPED_BYTES + 1))
    _, errors = read_client_workbook(buffer.getvalue())
    assert errors == ["The workbook is too large once unpacked."]


def test_rejects_workbook_with_xml_entities():
    assert openpyxl.DEFUSEDXML  # openpyxl silently goes without it if it's not installed
    original = zipfile.ZipFile(io.BytesIO(workbook_bytes(loans=[GOOD_LOAN])))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as patched:
        for name in original.namelist():
            data = original.read(name)
            if name == "xl/worksheets/sheet1.xml":
                data = data.replace(b"<worksheet", b'<!DOCTYPE worksheet [<!ENTITY a "b">]><worksheet', 1)
            patched.writestr(name, data)
    _, errors = read_client_workbook(buffer.getvalue(), today=TODAY)
    assert errors == ["The file could not be read as an Excel workbook."]


# ------------------------------------------------------------------
# Login
# ------------------------------------------------------------------

import time  # noqa: E402

import auth  # noqa: E402


@pytest.mark.parametrize("method, path", [
    ("get", "/"), ("post", "/price"), ("get", "/static/sample_client.xlsx"),
    ("get", "/static/results.js"), ("get", "/static/vendor/chart.umd.min.js"), ("get", "/not-a-page"),
])
def test_everything_but_the_login_page_requires_a_session(anonymous, method, path):
    response = getattr(anonymous, method)(path)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert response.headers["cache-control"] == "no-store"


def test_login_page_and_its_stylesheet_are_public(anonymous):
    assert anonymous.get("/login").status_code == 200
    assert anonymous.get("/static/style.css").status_code == 200


def test_wrong_password_is_refused(anonymous):
    response = anonymous.post("/login", data={"password": "not-the-password"})
    assert response.status_code == 401
    assert "Incorrect password" in response.text
    assert auth.COOKIE_NAME not in anonymous.cookies
    assert anonymous.get("/").status_code == 303


def test_correct_password_opens_the_app_with_a_protected_cookie(anonymous):
    response = anonymous.post("/login", data={"password": TEST_PASSWORD})
    assert response.status_code == 303 and response.headers["location"] == "/"
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert "max-age" not in cookie  # goes away when the browser closes
    assert "secure" not in cookie   # 127.0.0.1 has no HTTPS
    assert anonymous.get("/").status_code == 200
    assert "Log out" in anonymous.get("/").text


def test_cookie_is_https_only_away_from_this_computer(anonymous):
    import app
    from fastapi.testclient import TestClient

    remote = TestClient(app.app, base_url="https://pricing.example", follow_redirects=False)
    response = remote.post("/login", data={"password": TEST_PASSWORD})
    assert "secure" in response.headers["set-cookie"].lower()


def test_logging_out_ends_the_session_on_the_server(web):
    token = web.cookies[auth.COOKIE_NAME]
    assert web.post("/logout").headers["location"] == "/login"
    # Even a saved copy of the cookie no longer works
    web.cookies.clear()
    assert web.get("/", headers={"cookie": f"{auth.COOKIE_NAME}={token}"}).status_code == 303


def test_idle_session_expires(web):
    auth._sessions[web.cookies[auth.COOKIE_NAME]] = time.monotonic() - 1
    assert web.get("/").status_code == 303


def test_made_up_cookie_is_refused(anonymous):
    assert anonymous.get("/", headers={"cookie": f"{auth.COOKIE_NAME}=forged"}).status_code == 303


def test_repeated_failures_lock_out_even_the_right_password(monkeypatch, anonymous):
    for _ in range(auth.MAX_FAILED_LOGINS):
        assert anonymous.post("/login", data={"password": "wrong"}).status_code == 401
    assert anonymous.post("/login", data={"password": TEST_PASSWORD}).status_code == 429

    monkeypatch.setattr(auth, "LOCKOUT_SECONDS", 0)  # the lockout has passed
    assert anonymous.post("/login", data={"password": TEST_PASSWORD}).status_code == 303


@pytest.mark.parametrize("password", ["", "change_me"])
def test_nobody_can_log_in_without_a_real_password_set(monkeypatch, anonymous, password):
    monkeypatch.setenv("APP_PASSWORD", password)
    assert "APP_PASSWORD" in anonymous.get("/login").text
    response = anonymous.post("/login", data={"password": password})
    assert response.status_code == 503
    assert auth.COOKIE_NAME not in anonymous.cookies


def test_oversized_login_request_is_refused_cleanly(anonymous):
    response = anonymous.post("/login", content=b"a" * (4 * 1024 * 1024),
                              headers={"content-type": "application/x-www-form-urlencoded"})
    assert response.status_code == 413
