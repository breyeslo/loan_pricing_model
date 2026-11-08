# Loan Pricing Model

A small web app that prices a new commercial loan two ways:

1. **Standalone**: what rate does this loan need to earn the bank's hurdle return on its own?
2. **Relationship**: given the borrower's existing loans, deposits and fees with the bank, what rate does the new loan need so that the **whole relationship** earns the hurdle?

It pulls today's Treasury curve, SOFR and Prime from the [FRED API](https://fred.stlouisfed.org/), builds a funds transfer pricing (FTP) curve, and adds liquidity, prepayment-option, credit, operating-cost and capital charges.

> **Illustrative model with synthetic data and assumptions. Not any bank's actual methodology.**
> All assumptions live in [`config.yaml`](config.yaml) and are made-up but plausible values.

## The pricing equation

All rates are annual, on the loan balance. Results are year-1 on current balances.

```
Required rate = FTP base + liquidity premium + option cost + expected loss + opex
              + capital charge - fee credit - floor credit
```

| Component | How it's calculated |
|---|---|
| **FTP base** (fixed rate) | Strip funding: each principal repayment `P_i` at time `t_i` is funded at the curve rate for that maturity. `FTP = Σ P_i·t_i·curve(t_i) / Σ P_i·t_i`. The curve is the Treasury curve plus a funding spread. |
| **FTP base** (floating) | SOFR + funding spread, for SOFR and Prime loans alike. The loan rate is the higher of index + margin and the floor. |
| **Liquidity premium** | Interpolated from a term grid in `config.yaml`. |
| **Option cost** | Fixed-rate loans with no prepayment penalty only. Borrowers prepay at an annual CPR set by collateral (monthly `SMM = 1 − (1 − CPR)^(1/12)`). A prepayment after rates have fallen leaves the bank reinvesting at the lower rate for the life the loan had left. With rates moving randomly at volatility `σ`, the expected fall after `t` years is `σ·√t·0.399`. So `option cost = Σ prepaid_t × σ√t·0.399 × remaining life_t / Σ expected balance_t × (1/12)`. See `option_cost()`. |
| **Expected loss** | PD (by risk grade) × LGD (by collateral). |
| **Opex** | A flat annual cost rate. |
| **Capital charge** | `k × (hurdle / (1 − tax) − FTP)`, where `k = capital ratio × risk weight`. Equity must earn the hurdle after tax, net of the funding it saves (equity doesn't have to be borrowed at FTP). |
| **Fee credit** | `(upfront fee / term in years) / balance` |
| **Floor credit** | New floating deals with a floor. The balance-weighted value of a put on the index struck at `floor − margin`, using the same `σ` as the option cost, less any amount the floor already adds today. See `floor_credit()`. |

**ROE** (return on the equity allocated to the loan) at any rate `r`:

```
pre-tax income = (r − FTP − LP − option − EL − opex + floor credit) × B + upfront fee / term + k × B × FTP
ROE            = pre-tax income × (1 − tax) / (k × B)
```

At the required rate, ROE equals the hurdle exactly (the tests check this).

**Relationship view.**

- **Existing loans** are priced with the same functions at their actual rates. For fixed-rate loans, the FTP rate is **locked at origination**: the loan's *original* schedule (origination to maturity) is strip-funded on the Treasury curve from its origination month. That curve is the monthly average from FRED, plus today's funding spread. Liquidity premium and option cost also use the original term. Floating loans reprice off today's SOFR.
- **Deposits** are split into a **stable core and a volatile part**. The stable share is credited at the curve rate for its assumed life. The volatile share is credited at the 1-month rate: `credit = s·curve(stable life) + (1 − s)·curve(1 month)`. CDs are credited 100% at their remaining term. Income is the credit minus interest paid, earnings credit and opex. No capital is held against deposits.
- **Fees** from treasury management add straight to income.

The deal's income is linear in its rate, so the **relationship-adjusted required rate** (where total relationship ROE equals the hurdle) is solved in closed form. See `relationship_required_rate()` in [`pricing.py`](pricing.py).

## Files

| File | What it does |
|---|---|
| [`pricing.py`](pricing.py) | All the pricing math. Read it top to bottom. |
| [`market_data.py`](market_data.py) | Pulls today's curve and the monthly history from FRED, caches both, interpolates the curve |
| [`data_input.py`](data_input.py) | Reads and validates the Excel upload and the deal form |
| [`app.py`](app.py) | FastAPI routes (a thin layer) |
| [`auth.py`](auth.py) | The login: password check, sessions, and the rule that every page needs one |
| [`config.yaml`](config.yaml) | Every FTP, credit and capital assumption |
| [`make_sample_data.py`](make_sample_data.py) | Builds `static/template.xlsx` and `static/sample_client.xlsx` |
| [`tests/test_pricing.py`](tests/test_pricing.py) | Tests. They use a fixed fake curve and make no network calls. |

## Run it locally

1. Get a free FRED API key: <https://fred.stlouisfed.org/docs/api/api_key.html>
2. Copy `.env.example` to `.env`. Paste in your key and choose a login password of at least 12 characters:
   ```
   FRED_API_KEY=your_key_here
   APP_PASSWORD=change_me
   ```
3. Install, generate the sample workbooks, and start the server:
   ```
   pip install -r requirements.txt
   python make_sample_data.py
   uvicorn app:app --reload
   ```
4. Open <http://127.0.0.1:8000> and log in with your password. Tick **Use sample client** and click **Price this deal**.

Run the tests with `pytest`.

Today's curve is cached for the day in `cache/market_YYYY-MM-DD.json`. The monthly Treasury history since 2001, used for origination-date FTP, is fetched in one call per series and cached for the month in `cache/fred_history_YYYY-MM.json`.

If FRED can't be reached, the app uses the most recent cache file and shows a warning. If there's no cached curve at all, it shows an error. If only the history is missing, existing loans fall back to today's curve, with a warning.

## Upload format

An `.xlsx` file (max 2 MB, read in memory and never saved) with three sheets. Any sheet may be empty. Enter rates as percents (6.25 means 6.25%).

- **Loans:** loan_id, loan_type (CI/CRE/Construction), balance, rate_type (fixed/floating), rate (the current all-in rate), origination_date (2001-08-01 or later), maturity_date, amortization (bullet/level_payment), amortization_months (remaining), risk_grade (1–8), collateral_type, prepay_penalty (Y/N)
- **Deposits:** account_id, account_type (dda/interest_checking/savings/high_yield_savings/mmda/cd), avg_balance, rate_paid, ecr_rate, cd_maturity_date (CDs only)
- **Fees:** fee_type, annual_amount

The validator reports every problem with its Excel row number, for example `Loans row 4: maturity_date is in the past.`

## Security and client data

The app is designed to hold client information only for as long as it takes to price one deal. It has no database, and it saves nothing about a client.

**Login**

- Everything except the login page requires logging in, including the downloadable workbooks.
- There is one shared password, set as `APP_PASSWORD` (at least 12 characters). If it isn't set, nobody can log in.
- You're logged out after 30 minutes of inactivity, when you close the browser, or when the server restarts.
- Five wrong passwords from the same address block further attempts for 5 minutes.
- The login cookie can't be read by scripts on the page or used by form posts from other sites. Away from your own computer, it's only sent over HTTPS.

**Where client data goes**

| Place | What happens |
|---|---|
| **Upload** | Read into memory for one request, then discarded. Requests over 3 MB are cut off as the bytes arrive, which keeps every upload below the size at which the web framework would write it to a temp file. |
| **`cache/` folder** | Public FRED interest rates only. The code that writes it never receives client data, and a test confirms a client's details don't end up there. |
| **FRED** | The app requests rate series and dates, over HTTPS. No client data is sent. |
| **Browser** | Every response is sent with `Cache-Control: no-store`, so results aren't kept in the browser's cache. Form autofill is off. |
| **Third parties** | None. Chart.js is served from this app, and a Content-Security-Policy stops the page loading from, or sending data to, any other site. |
| **Logs** | The app writes no logs of its own. Uvicorn's access log records the path and status, not the form or file. |

**Handling untrusted workbooks**

- An upload must be a real `.xlsx` of at most 2 MB, and at most 50 MB once unpacked, which blocks "zip bombs".
- XML is parsed with `defusedxml`, which rejects the entity tricks used in XML attacks.
- Macros are never run, and cell values are treated as data.
- Values are autoescaped when displayed, so text in a file can't inject code into the page.

**What the app can't protect**

- **One shared password.** The app can't tell users apart or record who priced what. Anyone with the password has full access, but can't see other people's results, because none are stored.
- **No encryption in transit.** Uvicorn serves plain HTTP, so away from your own computer, HTTPS must be added in front. Without it, the login won't stay signed in.
- **Memory isn't wiped.** Python frees the data but doesn't overwrite it, so it could appear in an operating-system page file or a crash dump.
- **Copies outside the app.** The original workbook, and any PDF printed from the results page, are the user's to protect. Filenames appear on the results page, so they may reveal a client's name.

**Before using real client data**

This is a demonstration model. The protections above help, but on their own they don't make it ready for real client information. Before anyone uses it that way:

1. **Get it approved.** Ask your information security and compliance teams to review how it will be used.
2. **Keep it private.** Run it on your own computer, or have IT host it on a private, secure connection. Never put it on the open internet.
3. **Protect the password.** Make it long and unique, share it only with people who need it, and change it when someone leaves. Keep the `.env` file that stores it private.
4. **Keep client files somewhere safe.** Store workbooks and printed results where your organization keeps client records, not in this project folder.
5. **Ask IT to look after the setup.** If IT hosts the app, share this section with them. It needs up-to-date software and an encrypted computer or server, and nothing in front of the app should save uploaded files.

## Simplifications and possible next steps

- **Unused commitment capital.** Only drawn balances carry capital. Undrawn lines would need capital too.
- **Live what-if sliders.** Re-price instantly as rate, fee or term change.
- Also simplified: year-1 annualized view only (no multi-year NPV), no day counts, a flat tax rate, and the deal's amortization schedule is held fixed at the offered rate when solving for the relationship rate.
