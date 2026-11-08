"""
Market data: pull Treasury yields, SOFR and Prime from FRED, cache them for the day.
All rates are decimals.
"""

import json
from datetime import date, timedelta
from pathlib import Path

import requests

FRED_URL = "https://api.stlouisfed.org/fred/series/observations"
CACHE_DIR = Path(__file__).parent / "cache"

# Treasury constant-maturity series and their tenor in years
TREASURY_SERIES = {
    "DGS1MO": 1 / 12,
    "DGS3MO": 0.25,
    "DGS6MO": 0.5,
    "DGS1": 1,
    "DGS2": 2,
    "DGS3": 3,
    "DGS5": 5,
    "DGS7": 7,
    "DGS10": 10,
    "DGS20": 20,
    "DGS30": 30,
}
SOFR_SERIES = "SOFR"
PRIME_SERIES = "DPRIME"
LOOKBACK_DAYS = 14
PRIME_LOOKBACK_DAYS = 60


class MarketDataError(Exception):
    """Raised when there is no live data and no cache to fall back on."""


# ------------------------------------------------------------------
# Curve math
# ------------------------------------------------------------------

def interpolate(points, x):
    """
    Linear interpolation
    """
    if x <= points[0][0]:
        return points[0][1]
    if x >= points[-1][0]:
        return points[-1][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x0 <= x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)


def build_funding_curve(market, funding_spread_bps):
    spread = funding_spread_bps / 10000
    return [(p["tenor"], p["rate"] + spread) for p in market["treasury"]]


def curve_rate(curve, t_years):
    return interpolate(curve, t_years)


# ------------------------------------------------------------------
# FRED download
# ------------------------------------------------------------------

def fetch_latest(series_id, api_key, today, lookback_days=LOOKBACK_DAYS):
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "observation_start": (today - timedelta(days=lookback_days)).isoformat(),
        "observation_end": today.isoformat(),
    }
    resp = requests.get(FRED_URL, params=params, timeout=10)
    resp.raise_for_status()
    observations = [o for o in resp.json()["observations"] if o["value"] != "."]
    if not observations:
        raise MarketDataError(f"FRED returned no recent values for {series_id}.")
    latest = observations[-1]  
    return float(latest["value"]) / 100, latest["date"]


def fetch_from_fred(api_key, today):
    """Download every series and return one market-data dict."""
    treasury = []
    for series_id, tenor in TREASURY_SERIES.items():
        rate, obs_date = fetch_latest(series_id, api_key, today)
        treasury.append({"series": series_id, "tenor": tenor, "rate": rate, "date": obs_date})
    sofr, sofr_date = fetch_latest(SOFR_SERIES, api_key, today)
    prime, prime_date = fetch_latest(PRIME_SERIES, api_key, today, PRIME_LOOKBACK_DAYS)
    return {
        "as_of": max(p["date"] for p in treasury),
        "treasury": treasury,
        "sofr": sofr,
        "sofr_date": sofr_date,
        "prime": prime,
        "prime_date": prime_date,
    }


# ------------------------------------------------------------------
# Monthly rate history (for origination-date FTP)
# ------------------------------------------------------------------

HISTORY_START = "2001-08-01"  


def fetch_monthly_history(api_key, today):
    """
    Monthly-average Treasury yields since 2001: one FRED call per series.

    """
    months = {}
    for series_id in TREASURY_SERIES:
        params = {
            "series_id": series_id,
            "api_key": api_key,
            "file_type": "json",
            "observation_start": HISTORY_START,
            "observation_end": today.isoformat(),
            "frequency": "m",
            "aggregation_method": "avg",
        }
        resp = requests.get(FRED_URL, params=params, timeout=20)
        resp.raise_for_status()
        for obs in resp.json()["observations"]:
            if obs["value"] != ".":
                months.setdefault(obs["date"][:7], {})[series_id] = float(obs["value"]) / 100
    return {"as_of": max(months), "months": months}


def curve_for_month(history, month):
    """
    The Treasury curve for a "YYYY-MM" month.
    """
    on_or_before = [m for m in history["months"] if m <= month]
    used = max(on_or_before) if on_or_before else min(history["months"])
    values = history["months"][used]
    treasury = [{"series": s, "tenor": tenor, "rate": values[s]}
                for s, tenor in TREASURY_SERIES.items() if s in values]
    return {"as_of": used, "treasury": treasury}


# ------------------------------------------------------------------
# Cache with fallback (used for both today's curve and the history)
# ------------------------------------------------------------------

def load_with_cache(cache_dir, file_name, fallback_pattern, api_key, fetch, what):
    """
    1. If this period's cache file exists, use it.
    2. Otherwise pull from FRED and save the cache file.
    3. If FRED fails, fall back to the most recent cache file, with a warning.
    4. If there is no cache at all, raise MarketDataError.
    
    """
    cache_dir = Path(cache_dir)
    cache_file = cache_dir / file_name
    if cache_file.exists():
        data = json.loads(cache_file.read_text())
        data["warning"] = None
        return data

    problem = "FRED_API_KEY is not set"
    if api_key:
        try:
            data = fetch()
            cache_dir.mkdir(exist_ok=True)
            cache_file.write_text(json.dumps(data, indent=2))
            data["warning"] = None
            return data
        except Exception as exc:  # network error, bad key, FRED outage...
            problem = f"FRED could not be reached ({type(exc).__name__})"

    cached = sorted(cache_dir.glob(fallback_pattern)) if cache_dir.exists() else []
    if not cached:
        raise MarketDataError(
            f"No {what} available: {problem} and there is no cached data. "
            "Set FRED_API_KEY in your .env file (or host environment) and try again."
        )
    data = json.loads(cached[-1].read_text())
    data["warning"] = f"Live {what} unavailable: {problem}. Using cached {what} as of {data['as_of']}."
    return data


def get_market_data(api_key, today=None, cache_dir=CACHE_DIR, fetch=fetch_from_fred):
    """Today's Treasury curve, SOFR and Prime, cached for the day."""
    today = today or date.today()
    return load_with_cache(cache_dir, f"market_{today.isoformat()}.json", "market_[0-9]*.json",
                           api_key, lambda: fetch(api_key, today), "market data")


def get_rate_history(api_key, today=None, cache_dir=CACHE_DIR, fetch=fetch_monthly_history):
    """Monthly Treasury history since 2001, cached for the month."""
    today = today or date.today()
    return load_with_cache(cache_dir, f"fred_history_{today:%Y-%m}.json", "fred_history_*.json",
                           api_key, lambda: fetch(api_key, today), "rate history")
