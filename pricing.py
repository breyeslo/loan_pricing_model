"""
Loan pricing math:

  1. Cash flows       - the loan's monthly principal repayment schedule
  2. FTP base rate    - what the bank's treasury charges to fund the loan
  3. Option cost      - what the borrower's free right to prepay costs the bank
  4. Components       - liquidity, option, credit, opex, capital, fees
                        and the required rate that earns exactly the hurdle
  5. Contribution     - income, equity and ROE at a given rate
  6. Existing loans   - priced on the FTP rate locked in at origination
  7. Deposits & fees  - what the rest of the relationship earns
  8. Relationship     - the rate that makes the WHOLE relationship hit the hurdle

Conventions: every rate is an annual decimal (0.0625 = 6.25%).
Time is in years = months / 12. 
Results are year-1 annualized on current balances.
"""

import math
from pathlib import Path
from statistics import NormalDist

import yaml

from market_data import build_funding_curve, curve_for_month, curve_rate, interpolate

CONFIG_PATH = Path(__file__).parent / "config.yaml"


def load_config(path=CONFIG_PATH):
    with open(path) as f:
        return yaml.safe_load(f)


def bps(value):
    """Basis points to decimal: 25 bp -> 0.0025."""
    return value / 10000


# ------------------------------------------------------------------
# 1. Cash flows
# ------------------------------------------------------------------

def principal_schedule(balance, term_months, annual_rate, amortization, amortization_months):
    """
    Return a list of (time_in_years, principal_repaid), one per repayment.

    - bullet: interest only, all principal repaid at maturity.
    - level_payment: a fixed monthly payment that would pay the loan off over
      amortization_months. If the term is shorter than the amortization,
      whatever is still owed is repaid as a balloon at maturity.
    """
    if amortization == "bullet":
        return [(term_months / 12, balance)]

    monthly_rate = annual_rate / 12
    if monthly_rate == 0:
        payment = balance / amortization_months
    else:
        payment = balance * monthly_rate / (1 - (1 + monthly_rate) ** -amortization_months)

    schedule = []
    remaining = balance
    for month in range(1, term_months + 1):
        principal = payment - remaining * monthly_rate  
        if month == term_months:
            principal = remaining  
        schedule.append((month / 12, principal))
        remaining -= principal
    return schedule


def loan_schedule(loan):
    return principal_schedule(loan["balance"], loan["term_months"], loan["rate"],
                              loan["amortization"], loan["amortization_months"])


# ------------------------------------------------------------------
# 2. FTP base rate
# ------------------------------------------------------------------

def strip_ftp(schedule, curve):
    """
    The loan's FTP rate is the average of those curve
    rates, weighted by principal x time outstanding (dollar-years of funding);
    for a bullet loan there is one repayment, so FTP = curve(maturity).

    """
    weighted_rates = sum(p * t * curve_rate(curve, t) for t, p in schedule)
    dollar_years = sum(p * t for t, p in schedule)
    return weighted_rates / dollar_years


def ftp_base_rate(loan, curve, sofr, cfg):
    """
    Fixed-rate loans are strip funded off the curve.
    Floating-rate loans are funded at SOFR + funding spread.
    """
    if loan["rate_type"] == "floating":
        return sofr + bps(cfg["funding_spread_bps"])
    return strip_ftp(loan_schedule(loan), curve)


# ------------------------------------------------------------------
# 3. Option cost (prepayment)
# ------------------------------------------------------------------

def remaining_average_life(schedule, t):
    """Average years until the principal still owed after time t is repaid."""
    later = [(s, p) for s, p in schedule if s > t]
    owed = sum(p for _, p in later)
    return sum(p * (s - t) for s, p in later) / owed if owed else 0.0


def option_cost(loan, cfg):
    """
    Option-adjusted cost of the borrower's free right to prepay a fixed-rate loan.
    Zero for floating loans (they reprice anyway) and for loans with a
    prepayment penalty (the penalty makes the bank whole).

    - Borrowers prepay at an annual CPR that depends on collateral. The
      equivalent monthly rate is SMM = 1 - (1 - CPR)^(1/12).
    - A borrower who prepays after rates have fallen leaves the bank
      reinvesting that money at the lower rate for the life the loan had left.
    - If rates move randomly with annual volatility sigma, the expected fall
      after t years (rises cost the bank nothing) is sigma * sqrt(t) * 0.399,
      the average of the positive half of a normal distribution.

        cost of prepayments = sum(prepaid_t * expected_fall_t * remaining_life_t)
        option cost         = cost of prepayments / expected dollar-years outstanding
    """
    if loan["rate_type"] != "fixed" or loan["prepay_penalty"]:
        return 0.0

    cpr = cfg["cpr_by_collateral"][loan["collateral_type"]]
    sigma = bps(cfg["rate_volatility_bps"])
    smm = 1 - (1 - cpr) ** (1 / 12)
    expected_fall_factor = 1 / math.sqrt(2 * math.pi)  # = 0.399

    schedule = loan_schedule(loan)
    repaid_in_month = {round(t * 12): p for t, p in schedule}
    months = loan["term_months"]

    contractual = loan["balance"]  
    still_on_books = 1.0           
    cost = 0.0
    dollar_years = 0.0
    for month in range(1, months + 1):
        t = month / 12
        expected_balance = contractual * still_on_books
        dollar_years += expected_balance / 12
        if month < months:  
            prepaid = expected_balance * smm
            expected_fall = sigma * math.sqrt(t) * expected_fall_factor
            cost += prepaid * expected_fall * remaining_average_life(schedule, t)
            still_on_books *= 1 - smm
        contractual -= repaid_in_month.get(month, 0.0)
    return cost / dollar_years


def floor_credit(loan, cfg):
    """Time value of a rate floor: a put on the index, struck at floor - margin."""
    floor = loan.get("rate_floor")
    sigma = bps(cfg["rate_volatility_bps"])
    if loan["rate_type"] != "floating" or not floor or not sigma:
        return 0.0

    in_the_money = floor - loan["float_margin"] - loan["index_rate"]
    normal = NormalDist()
    repaid_in_month = {round(t * 12): p for t, p in loan_schedule(loan)}
    balance = loan["balance"]
    value = dollars = 0.0
    for month in range(1, loan["term_months"] + 1):
        s = sigma * math.sqrt(month / 12)
        z = in_the_money / s
        value += balance * (in_the_money * normal.cdf(z) + s * normal.pdf(z))
        dollars += balance
        balance -= repaid_in_month.get(month, 0.0)
    return value / dollars - max(in_the_money, 0.0)


# ------------------------------------------------------------------
# 4. Components and the required rate
# ------------------------------------------------------------------

def capital_factor(loan, cfg):
    """k = equity held per $1 of loan = capital ratio x risk weight."""
    return cfg["capital_ratio"] * cfg["risk_weights"][loan["loan_type"]]


def loan_components(loan, curve, sofr, cfg):
    """
    Break the required rate into its building blocks (all annual decimals).
    """
    term_years = loan["term_months"] / 12
    k = capital_factor(loan, cfg)
    hurdle, tax = cfg["hurdle_roe"], cfg["tax_rate"]

    ftp = ftp_base_rate(loan, curve, sofr, cfg)

    # Liquidity premium: longer loans lock up funding longer
    lp_grid = sorted((float(t), bps(v)) for t, v in cfg["liquidity_premium_bps"].items())
    liquidity = interpolate(lp_grid, term_years)

    option = option_cost(loan, cfg)
    floor = floor_credit(loan, cfg)

    # Expected loss = probability of default x loss given default
    expected_loss = (cfg["pd_by_risk_grade"][loan["risk_grade"]]
                     * cfg["lgd_by_collateral"][loan["collateral_type"]])

    opex = bps(cfg["loan_opex_bps"])

    capital_charge = k * (hurdle / (1 - tax) - ftp)

    # Upfront fee spread evenly over the life of the loan
    fee_credit = (loan.get("upfront_fee", 0.0) / term_years) / loan["balance"]

    required = ftp + liquidity + option + expected_loss + opex + capital_charge - fee_credit - floor

    return {
        "ftp": ftp,
        "liquidity": liquidity,
        "option": option,
        "expected_loss": expected_loss,
        "opex": opex,
        "capital_charge": capital_charge,
        "fee_credit": fee_credit,
        "floor_credit": floor,
        "required_rate": required,
        "k": k,
    }


# ------------------------------------------------------------------
# 5. Contribution at a given rate
# ------------------------------------------------------------------

def roe(after_tax_income, capital):
    """Return on equity: after-tax income / equity allocated. None when no equity is held."""
    return after_tax_income / capital if capital else None


def loan_contribution(loan, comp, cfg, rate=None):
    """
    Year-1 income, capital and ROE on a loan at `rate` (default: its own rate).

        pretax = (rate - FTP - LP - option - EL - opex) * B     spread income
               + upfront_fee / term_years                       amortized fee
               + k * B * FTP                                    funding benefit of equity
    """
    r = loan["rate"] if rate is None else rate
    balance = loan["balance"]
    term_years = loan["term_months"] / 12
    net_spread = (r - comp["ftp"] - comp["liquidity"] - comp["option"]
                  - comp["expected_loss"] - comp["opex"] + comp["floor_credit"])
    pretax = (net_spread * balance
              + loan.get("upfront_fee", 0.0) / term_years
              + comp["k"] * balance * comp["ftp"])
    capital = comp["k"] * balance
    after_tax = pretax * (1 - cfg["tax_rate"])
    return {"balance": balance, "pretax": pretax, "after_tax": after_tax,
            "capital": capital, "roe": roe(after_tax, capital)}


# ------------------------------------------------------------------
# 6. Existing loans: FTP locked at origination
# ------------------------------------------------------------------

def as_originated(loan):
    """
    The loan as it looked when booked: original term and amortization are the
    remaining ones plus the months already elapsed. (The original balance is
    not needed: the FTP rate depends on the schedule's shape, not its size.)
    """
    elapsed = loan["elapsed_months"]
    level = loan["amortization"] == "level_payment"
    return {**loan,
            "term_months": loan["term_months"] + elapsed,
            "amortization_months": loan["amortization_months"] + elapsed if level else 0}


def existing_loan_contribution(loan, today_curve, sofr, history, cfg):
    """
    Treasury locks a fixed-rate loan's FTP rate on the day it is booked, so an
    existing loan is charged the curve from its ORIGINATION month, over its
    ORIGINAL schedule. Liquidity premium and option cost are set the same way.
    Floating loans still reprice off today's SOFR.

    Without rate history (FRED unavailable), fall back to today's curve and
    the remaining term.
    """
    if history is None:
        comp = loan_components(loan, today_curve, sofr, cfg)
        curve_month = None
    else:
        month_market = curve_for_month(history, loan["origination_date"].strftime("%Y-%m"))
        curve = build_funding_curve(month_market, cfg["funding_spread_bps"])
        comp = loan_components(as_originated(loan), curve, sofr, cfg)
        curve_month = month_market["as_of"]
    row = loan_contribution(loan, comp, cfg)
    row.update(loan_id=loan.get("loan_id", ""), loan_type=loan["loan_type"],
               rate_type=loan["rate_type"], rate=loan["rate"], ftp=comp["ftp"],
               option=comp["option"], curve_month=curve_month,
               origination_date=loan.get("origination_date"))
    return row


# ------------------------------------------------------------------
# 7. Deposits and fees
# ------------------------------------------------------------------

def deposit_ftp_credit(deposit, curve, cfg):
    """
    Return (ftp_credit_rate, stable_share).

    Non-maturity deposits are split into a stable core and a volatile part.
    The stable core behaves like long-term funding, so it is credited at the
    curve rate for its assumed life; the volatile part could leave any day,
    so it only earns the short-end rate:

        credit = stable_share * curve(stable life) + (1 - stable_share) * curve(volatile life)

    A CD is all stable: credited at the curve rate for its remaining term.
    """
    if deposit["account_type"] == "cd":
        return curve_rate(curve, deposit["cd_term_months"] / 12), 1.0
    behavior = cfg["deposit_behavior"][deposit["account_type"]]
    stable = behavior["stable_share"]
    credit = (stable * curve_rate(curve, behavior["stable_life_years"])
              + (1 - stable) * curve_rate(curve, cfg["volatile_life_years"]))
    return credit, stable


def deposit_contribution(deposit, curve, cfg):
    """
    Deposits are a source of funds, so treasury pays the business an FTP
    credit. Income is that credit minus what the bank pays the client
    (interest + earnings credit on analyzed accounts) minus opex.
    No capital is held against deposits.
    """
    ftp_credit, stable_share = deposit_ftp_credit(deposit, curve, cfg)
    margin = ftp_credit - deposit["rate_paid"] - deposit["ecr_rate"] - bps(cfg["deposit_opex_bps"])
    pretax = margin * deposit["avg_balance"]
    return {"balance": deposit["avg_balance"], "pretax": pretax,
            "after_tax": pretax * (1 - cfg["tax_rate"]), "capital": 0.0, "roe": None,
            "account_id": deposit.get("account_id", ""), "account_type": deposit["account_type"],
            "ftp_credit": ftp_credit, "stable_share": stable_share,
            "rate_paid": deposit["rate_paid"], "ecr_rate": deposit["ecr_rate"]}


# ------------------------------------------------------------------
# 8. Relationship view
# ------------------------------------------------------------------

def summary_row(label, rows, cfg, balance=True):
    """Add up a list of contribution rows into one table row."""
    pretax = sum(r["pretax"] for r in rows)
    capital = sum(r["capital"] for r in rows)
    after_tax = pretax * (1 - cfg["tax_rate"])
    return {
        "label": label,
        "balance": sum(r["balance"] for r in rows) if balance else None,
        "pretax": pretax,
        "after_tax": after_tax,
        "capital": capital,
        "roe": roe(after_tax, capital),
    }


def relationship_required_rate(deal, comp, other_pretax, other_capital, cfg):
    """
    The new-deal rate r at which the TOTAL relationship ROE equals the hurdle.

    Deal pretax income is linear in r:  B * r + c,  where c is its income at r = 0.
    Solve  (other_pretax + B*r + c) * (1 - tax) = hurdle * (other_capital + k*B)  for r.
    """
    balance = deal["balance"]
    c = loan_contribution(deal, comp, cfg, rate=0.0)["pretax"]
    total_capital = other_capital + comp["k"] * balance
    needed_pretax = cfg["hurdle_roe"] * total_capital / (1 - cfg["tax_rate"])
    return (needed_pretax - other_pretax - c) / balance


def price_deal(deal, loans, deposits, fees, market, cfg, history=None):
    """
    Price the proposed deal on its own and in the context of the client's
    existing loans, deposits and fees. `history` is the monthly Treasury history
    used to lock existing loans' FTP at origination (None = use today's curve).
    """
    curve = build_funding_curve(market, cfg["funding_spread_bps"])
    sofr = market["sofr"]

    # Offered rate
    index_rate = None
    if deal["rate_type"] == "fixed":
        offered = deal["fixed_rate"]
    else:
        index_rate = market["prime"] if deal["rate_index"] == "Prime" else sofr
        offered = max(index_rate + deal["float_margin"], deal["rate_floor"])
    deal = {**deal, "rate": offered, "index_rate": index_rate}

    # --- New deal on a standalone basis
    comp = loan_components(deal, curve, sofr, cfg)
    deal_row = loan_contribution(deal, comp, cfg)

    # --- Existing relationship
    loan_rows = [existing_loan_contribution(l, curve, sofr, history, cfg) for l in loans]
    deposit_rows = [deposit_contribution(d, curve, cfg) for d in deposits]
    fee_income = sum(f["annual_amount"] for f in fees)
    fee_rows = [{"balance": 0.0, "pretax": fee_income, "capital": 0.0}]

    table = [
        summary_row("Existing Loans", loan_rows, cfg),
        summary_row("Deposits", deposit_rows, cfg),
        summary_row("Fees", fee_rows, cfg, balance=False),
        summary_row("New Deal", [deal_row], cfg),
    ]
    table.append(summary_row("Total", table, cfg, balance=False))

    other_pretax = sum(row["pretax"] for row in table[:3])
    other_capital = sum(row["capital"] for row in table[:3])
    relationship_rate = relationship_required_rate(deal, comp, other_pretax, other_capital, cfg)

    return {
        "deal": deal,
        "offered_rate": offered,
        "sofr": sofr,
        "components": comp,
        "cpr": cfg["cpr_by_collateral"][deal["collateral_type"]],
        "standalone_rate": comp["required_rate"],
        "relationship_rate": relationship_rate,
        "standalone_roe": deal_row["roe"],
        "relationship_roe": table[-1]["roe"],
        "hurdle": cfg["hurdle_roe"],
        "spread_to_ftp": offered - comp["ftp"],
        "relationship_table": table,
        "loan_rows": loan_rows,
        "deposit_rows": deposit_rows,
        "curve": curve,
    }
