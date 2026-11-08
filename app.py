"""
Web layer only: read the inputs, call the pricing model, render the pages.
All the finance lives in pricing.py.

Run locally:  uvicorn app:app --reload
"""

import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.formparsers import MultiPartParser

import auth
import data_input
from market_data import MarketDataError, get_market_data, get_rate_history
from pricing import load_config, price_deal

load_dotenv()

BASE = Path(__file__).parent
SAMPLE_FILE = BASE / "static" / "sample_client.xlsx"
SAMPLE_NAME = "ABC LLC (sample client)"
CFG = load_config()

# Keep uploads in memory: Starlette spills files over spool_max_size to a temp
# file on disk. SecurityMiddleware stops every request at MAX_REQUEST_BYTES,
# which is below the spool size, so a client file can never reach the disk.
MAX_REQUEST_BYTES = 3 * 1024 * 1024
MultiPartParser.spool_max_size = 4 * 1024 * 1024

# Everything the pages need is served from this app, so the browser can be told
# to load nothing from anywhere else and to send data nowhere else.
SECURITY_HEADERS = [
    (b"cache-control", b"no-store"),  # results must not stay in the browser's cache
    (b"content-security-policy", b"default-src 'none'; script-src 'self'; style-src 'self'; "
                                 b"form-action 'self'; base-uri 'none'; frame-ancestors 'none'"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-content-type-options", b"nosniff"),
]


class RequestTooLarge(Exception):
    """The request body went over MAX_REQUEST_BYTES."""


class SecurityMiddleware:
    """
    Adds SECURITY_HEADERS to every response and enforces the request size limit.
    The limit counts the bytes actually received, because a chunked upload has
    no Content-Length header to check.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        received = 0

        async def counting_receive():
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            if received > MAX_REQUEST_BYTES:
                raise RequestTooLarge
            return message

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                message["headers"] = [*message.get("headers", []), *SECURITY_HEADERS]
            await send(message)

        await self.app(scope, counting_receive, send_with_headers)


# The app has no JSON API, so the interactive docs would only add attack surface
app = FastAPI(title="Loan Pricing Model", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(auth.AuthMiddleware)
app.add_middleware(SecurityMiddleware)  # added last so it wraps the login redirects too
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")


# ------------------------------------------------------------------
# Display formats: decimals in, text out
# ------------------------------------------------------------------

def pct(value):
    return "n/a" if value is None else f"{value * 100:.2f}%"


def bp(value):
    return f"{value * 10000:,.0f}"


def spread(value):
    return f"{value * 10000:+,.0f} bp"


def money(value):
    if value is None:
        return ""
    return f"-${-value:,.0f}" if value < 0 else f"${value:,.0f}"


def tenor_label(years):
    return f"{round(years * 12)} mo" if years < 1 else f"{years:g} yr"


templates.env.filters.update(pct=pct, bp=bp, spread=spread, money=money, tenor=tenor_label)


# ------------------------------------------------------------------
# Form defaults and dropdown choices
# ------------------------------------------------------------------

DEFAULT_DEAL = {
    "loan_type": "CRE", "amount": "4,000,000", "term_months": "60", "rate_type": "fixed",
    "fixed_rate": "6.50", "rate_index": "SOFR", "float_margin": "2.50", "rate_floor": "",
    "amortization": "level_payment",
    "amortization_months": "300", "risk_grade": "4", "collateral_type": "real_estate",
    "prepay_penalty": "N", "upfront_fee": "20,000",
}

CHOICES = {
    "loan_type": data_input.LOAN_TYPES,
    "rate_type": data_input.RATE_TYPES,
    "rate_index": data_input.RATE_INDEXES,
    "amortization": data_input.AMORTIZATION_TYPES,
    "risk_grade": data_input.RISK_GRADES,
    "collateral_type": data_input.COLLATERAL_TYPES,
    "prepay_penalty": ["N", "Y"],
}


def show_form(request, form, errors=(), status_code=200):
    return templates.TemplateResponse(
        request, "index.html",
        {"form": form, "errors": list(errors), "choices": CHOICES,
         "use_sample": form.get("use_sample", "on"), "sample_name": SAMPLE_NAME},
        status_code=status_code,
    )


# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------

def show_login(request, error=None, status_code=200):
    return templates.TemplateResponse(request, "login.html", {"error": error}, status_code=status_code)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if auth.session_is_valid(request.cookies.get(auth.COOKIE_NAME)):
        return RedirectResponse("/", status_code=303)
    if not auth.is_configured():
        return show_login(request, auth.NOT_CONFIGURED, 503)
    return show_login(request)


@app.post("/login", response_class=HTMLResponse)
async def login(request: Request):
    if not auth.is_configured():
        return show_login(request, auth.NOT_CONFIGURED, 503)
    address = request.client.host if request.client else "unknown"
    if auth.is_locked_out(address):
        return show_login(request, "Too many failed attempts. Please try again in a few minutes.", 429)
    try:
        form = await request.form()
    except RequestTooLarge:
        return show_login(request, "That request was too large.", 413)

    if not auth.password_matches(str(form.get("password", ""))):
        auth.record_failure(address)
        return show_login(request, "Incorrect password.", 401)

    auth.clear_failures(address)
    response = RedirectResponse("/", status_code=303)
    auth.set_session_cookie(response, request, auth.start_session())
    return response


@app.post("/logout")
async def logout(request: Request):
    auth.end_session(request.cookies.get(auth.COOKIE_NAME))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return response


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return show_form(request, DEFAULT_DEAL)


@app.post("/price", response_class=HTMLResponse)
async def price(request: Request):
    try:
        form = await request.form()
    except RequestTooLarge:
        return show_form(request, DEFAULT_DEAL, ["The upload is larger than 2 MB."], 413)

    deal_form = {field: str(form.get(field, "")) for field in DEFAULT_DEAL}
    deal_form["use_sample"] = "on" if form.get("use_sample") else ""
    deal, errors = data_input.parse_deal_form(deal_form)

    # Where the existing relationship comes from: sample file, upload, or nothing
    upload = form.get("client_file")
    client = {"loans": [], "deposits": [], "fees": []}
    if deal_form["use_sample"]:
        client, file_errors = data_input.read_client_workbook(SAMPLE_FILE.read_bytes(), SAMPLE_FILE.name)
        client_name = SAMPLE_NAME
    elif upload is not None and getattr(upload, "filename", ""):
        data = await upload.read()
        client, file_errors = data_input.read_client_workbook(data, upload.filename)
        client_name = upload.filename
    else:
        file_errors = []
        client_name = "New client (no existing relationship)"
    errors += file_errors

    if errors:
        return show_form(request, deal_form, errors, 400)

    api_key = os.getenv("FRED_API_KEY")
    try:
        market = get_market_data(api_key)
    except MarketDataError as exc:
        return show_form(request, deal_form, [str(exc)], 503)
    warnings = [market["warning"]] if market["warning"] else []

    # Existing loans are charged the FTP rate from their origination month.
    # If no rate history is available, they fall back to today's curve.
    history = None
    if client["loans"]:
        try:
            history = get_rate_history(api_key)
            if history["warning"]:
                warnings.append(history["warning"])
        except MarketDataError as exc:
            warnings.append(f"{exc} Existing loans are priced on today's curve instead "
                            "of the curve at origination.")

    results = price_deal(deal, client["loans"], client["deposits"], client["fees"],
                         market, CFG, history)

    comp = results["components"]
    chart = {
        "labels": ["FTP base", "Liquidity", "Option", "Expected loss", "Opex",
                   "Capital charge", "Fee credit", "Floor credit", "Required rate"],
        "values": [round(v * 10000, 1) for v in (
            comp["ftp"], comp["liquidity"], comp["option"], comp["expected_loss"],
            comp["opex"], comp["capital_charge"], -comp["fee_credit"], -comp["floor_credit"],
            comp["required_rate"])],
    }
    funding_spread = CFG["funding_spread_bps"] / 10000
    curve_rows = [{"series": p["series"], "tenor": p["tenor"], "treasury": p["rate"],
                   "funding": p["rate"] + funding_spread} for p in market["treasury"]]

    return templates.TemplateResponse(request, "results.html", {
        "r": results,
        "client_name": client_name,
        "counts": {k: len(v) for k, v in client.items()},
        "market": market,
        "warnings": warnings,
        "curve_rows": curve_rows,
        "chart": chart,
        "cfg": CFG,
    })
