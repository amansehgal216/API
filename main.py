"""
Indian Mutual Funds API
=======================
A small FastAPI service that serves data for (almost) every mutual fund in India:

  - latest NAV
  - point-to-point and annualised (CAGR) returns, computed from historical NAV
  - expense ratio (best-effort, from a community data source)

Data sources (all free, no API key required):
  - AMFI  (OFFICIAL)  : full scheme universe + latest NAV
        https://www.amfiindia.com/spages/NAVAll.txt
  - MFAPI (community) : full historical NAV per scheme (used to compute returns)
        https://api.mfapi.in/mf/{scheme_code}
  - MFData(community) : expense ratio / rating / ratios (best-effort enrichment)
        https://mfdata.in/api/v1/schemes/{scheme_code}

Design note:
  Listing *all* funds with NAV is one cheap call to AMFI. Returns + expense ratio
  require a per-scheme upstream lookup, so they are computed only on the detail
  endpoint (/funds/{code}) rather than for all ~12,000 schemes at once. This keeps
  the service fast and polite to the upstream sources.

Run:
  pip install -r requirements.txt
  uvicorn main:app --reload
  # then open http://127.0.0.1:8000/docs

Disclaimer: This wraps free community/official data feeds. Accuracy is not
guaranteed and this is not investment advice.
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timedelta
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
AMFI_NAV_ALL_URL = "https://www.amfiindia.com/spages/NAVAll.txt"
MFAPI_URL = "https://api.mfapi.in/mf/{code}"
MFDATA_URL = "https://mfdata.in/api/v1/schemes/{code}"

# How long cached data stays fresh (seconds)
AMFI_TTL = 60 * 60 * 6        # 6h  (NAV updates once per day on business days)
SCHEME_TTL = 60 * 60 * 12     # 12h (history changes at most once per day)
HTTP_TIMEOUT = 30.0

# Return periods, in approximate days. Periods >= 1y are also annualised (CAGR).
RETURN_PERIODS = {
    "1m": 30,
    "3m": 91,
    "6m": 182,
    "1y": 365,
    "3y": 365 * 3,
    "5y": 365 * 5,
}

# --------------------------------------------------------------------------- #
# Tiny in-memory TTL cache
# --------------------------------------------------------------------------- #
_cache: dict[str, tuple[float, object]] = {}


def cache_get(key: str):
    item = _cache.get(key)
    if item is None:
        return None
    expires_at, value = item
    if time.time() > expires_at:
        _cache.pop(key, None)
        return None
    return value


def cache_set(key: str, value, ttl: float):
    _cache[key] = (time.time() + ttl, value)


# --------------------------------------------------------------------------- #
# Pydantic response models (these also drive the Swagger docs at /docs)
# --------------------------------------------------------------------------- #
class FundSummary(BaseModel):
    scheme_code: int
    scheme_name: str
    fund_house: Optional[str] = None
    scheme_category: Optional[str] = None
    isin_growth: Optional[str] = None
    nav: Optional[float] = None
    nav_date: Optional[str] = None


class FundList(BaseModel):
    count: int
    page: int
    page_size: int
    total: int
    results: list[FundSummary]


class PeriodReturn(BaseModel):
    period: str
    start_date: Optional[str] = None
    start_nav: Optional[float] = None
    absolute_return_pct: Optional[float] = None
    annualised_return_pct: Optional[float] = None


class FundDetail(BaseModel):
    scheme_code: int
    scheme_name: str
    fund_house: Optional[str] = None
    scheme_category: Optional[str] = None
    isin_growth: Optional[str] = None
    nav: Optional[float] = None
    nav_date: Optional[str] = None
    expense_ratio: Optional[float] = None
    expense_ratio_source: Optional[str] = None
    rating: Optional[int] = None
    returns: list[PeriodReturn] = []


# --------------------------------------------------------------------------- #
# AMFI: full scheme universe + latest NAV
# --------------------------------------------------------------------------- #
async def fetch_amfi(client: httpx.AsyncClient) -> dict[int, FundSummary]:
    """Download and parse AMFI's NAVAll.txt into {scheme_code: FundSummary}."""
    cached = cache_get("amfi")
    if cached is not None:
        return cached

    resp = await client.get(AMFI_NAV_ALL_URL)
    resp.raise_for_status()
    text = resp.text

    funds: dict[int, FundSummary] = {}
    current_house: Optional[str] = None
    current_category: Optional[str] = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue

        if ";" not in line:
            # Header line: either a scheme-type/category or a fund-house name.
            lower = line.lower()
            if lower.startswith(("open ended", "close ended", "interval")):
                current_category = line
            else:
                current_house = line
            continue

        parts = line.split(";")
        # Expected: Code;ISIN Growth;ISIN ReInv;Name;NAV;Date
        if len(parts) < 6 or not parts[0].strip().isdigit():
            continue

        try:
            code = int(parts[0].strip())
            nav_raw = parts[4].strip()
            nav = float(nav_raw) if nav_raw not in ("", "N.A.", "-") else None
        except (ValueError, IndexError):
            continue

        funds[code] = FundSummary(
            scheme_code=code,
            scheme_name=parts[3].strip(),
            fund_house=current_house,
            scheme_category=current_category,
            isin_growth=(parts[1].strip() or None),
            nav=nav,
            nav_date=(parts[5].strip() or None),
        )

    if not funds:
        raise HTTPException(502, "Could not parse any funds from AMFI feed.")

    cache_set("amfi", funds, AMFI_TTL)
    return funds


# --------------------------------------------------------------------------- #
# MFAPI: historical NAV  ->  computed returns
# --------------------------------------------------------------------------- #
def _parse_history(data: list[dict]) -> list[tuple[datetime, float]]:
    out: list[tuple[datetime, float]] = []
    for row in data:
        try:
            d = datetime.strptime(row["date"], "%d-%m-%Y")
            out.append((d, float(row["nav"])))
        except (ValueError, KeyError, TypeError):
            continue
    out.sort(key=lambda x: x[0])  # oldest -> newest
    return out


def _nav_on_or_before(history: list[tuple[datetime, float]],
                      target: datetime,
                      tolerance_days: int = 10) -> Optional[tuple[datetime, float]]:
    """Nearest NAV at or just before `target` (markets close on weekends/holidays)."""
    best = None
    for d, nav in history:  # ascending
        if d <= target:
            best = (d, nav)
        else:
            break
    if best and (target - best[0]).days <= tolerance_days + 366:
        return best
    return None


def compute_returns(history: list[tuple[datetime, float]]) -> list[PeriodReturn]:
    results: list[PeriodReturn] = []
    if not history:
        return results

    latest_date, latest_nav = history[-1]

    for period, days in RETURN_PERIODS.items():
        target = latest_date - timedelta(days=days)
        if history[0][0] > target:  # not enough history for this period
            results.append(PeriodReturn(period=period))
            continue

        match = _nav_on_or_before(history, target)
        if not match or match[1] <= 0:
            results.append(PeriodReturn(period=period))
            continue

        start_date, start_nav = match
        absolute = (latest_nav / start_nav - 1) * 100

        # Annualise (CAGR) for periods of a year or more. Use the *measured*
        # span in the exponent; decide whether to annualise by the requested
        # period so a ~365-day lookback still counts as "1y".
        annualised = None
        years = (latest_date - start_date).days / 365.25
        if days >= 365 and years > 0:
            annualised = ((latest_nav / start_nav) ** (1 / years) - 1) * 100

        results.append(PeriodReturn(
            period=period,
            start_date=start_date.strftime("%d-%m-%Y"),
            start_nav=round(start_nav, 4),
            absolute_return_pct=round(absolute, 2),
            annualised_return_pct=round(annualised, 2) if annualised is not None else None,
        ))
    return results


async def fetch_returns(client: httpx.AsyncClient, code: int) -> list[PeriodReturn]:
    key = f"returns:{code}"
    cached = cache_get(key)
    if cached is not None:
        return cached
    try:
        resp = await client.get(MFAPI_URL.format(code=code))
        resp.raise_for_status()
        payload = resp.json()
        history = _parse_history(payload.get("data", []))
        result = compute_returns(history)
    except (httpx.HTTPError, ValueError):
        result = []
    cache_set(key, result, SCHEME_TTL)
    return result


# --------------------------------------------------------------------------- #
# MFData: expense ratio / rating (best-effort enrichment)
# --------------------------------------------------------------------------- #
async def fetch_expense(client: httpx.AsyncClient, code: int) -> dict:
    key = f"expense:{code}"
    cached = cache_get(key)
    if cached is not None:
        return cached
    info: dict = {}
    try:
        resp = await client.get(MFDATA_URL.format(code=code))
        resp.raise_for_status()
        data = resp.json().get("data", {}) or {}
        er = data.get("expense_ratio")
        info = {
            "expense_ratio": float(er) if er is not None else None,
            "expense_ratio_source": "mfdata.in" if er is not None else None,
            "rating": data.get("rating") or data.get("morningstar"),
        }
    except (httpx.HTTPError, ValueError, TypeError):
        info = {}
    cache_set(key, info, SCHEME_TTL)
    return info


# --------------------------------------------------------------------------- #
# FastAPI app
# --------------------------------------------------------------------------- #
app = FastAPI(
    title="Indian Mutual Funds API",
    version="1.0.0",
    description=(
        "Latest NAV, computed returns and (best-effort) expense ratio for Indian "
        "mutual funds. Sources: AMFI (NAV, official), mfapi.in (history), "
        "mfdata.in (expense ratio). Not investment advice."
    ),
)

# Allow your website to call this API from the browser. Set ALLOWED_ORIGINS to a
# comma-separated list of your site's URLs in production, e.g.
#   ALLOWED_ORIGINS="https://myapp.emergent.host,https://www.mysite.com"
# Leaving it as "*" allows any origin (fine for testing).
_origins = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",")]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_methods=["GET"],
    allow_headers=["*"],
)

_client: Optional[httpx.AsyncClient] = None


@app.on_event("startup")
async def _startup():
    global _client
    _client = httpx.AsyncClient(
        timeout=HTTP_TIMEOUT,
        headers={"User-Agent": "mf-api/1.0 (FastAPI)"},
        follow_redirects=True,
    )


@app.on_event("shutdown")
async def _shutdown():
    if _client:
        await _client.aclose()


@app.get("/", tags=["meta"])
async def root():
    return {
        "name": "Indian Mutual Funds API",
        "endpoints": {
            "GET /funds": "list/search all funds with latest NAV (paginated)",
            "GET /funds/{scheme_code}": "full detail: NAV + returns + expense ratio",
            "GET /funds/{scheme_code}/nav": "latest NAV only",
            "GET /funds/{scheme_code}/returns": "computed returns only",
            "GET /health": "service + upstream health",
        },
        "docs": "/docs",
        "disclaimer": "Free community/official data. Not investment advice.",
    }


@app.get("/health", tags=["meta"])
async def health():
    try:
        funds = await fetch_amfi(_client)
        return {"status": "ok", "fund_count": len(funds)}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"AMFI source unavailable: {exc}")


@app.get("/funds", response_model=FundList, tags=["funds"])
async def list_funds(
    search: Optional[str] = Query(None, description="Substring match on scheme name"),
    fund_house: Optional[str] = Query(None, description="Substring match on fund house"),
    category: Optional[str] = Query(None, description="Substring match on category"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
):
    """All funds + latest NAV (single cheap call to AMFI). Filter and paginate."""
    funds = await fetch_amfi(_client)
    items = list(funds.values())

    if search:
        s = search.lower()
        items = [f for f in items if s in f.scheme_name.lower()]
    if fund_house:
        h = fund_house.lower()
        items = [f for f in items if f.fund_house and h in f.fund_house.lower()]
    if category:
        c = category.lower()
        items = [f for f in items if f.scheme_category and c in f.scheme_category.lower()]

    total = len(items)
    start = (page - 1) * page_size
    page_items = items[start:start + page_size]

    return FundList(
        count=len(page_items),
        page=page,
        page_size=page_size,
        total=total,
        results=page_items,
    )


@app.get("/funds/{scheme_code}", response_model=FundDetail, tags=["funds"])
async def fund_detail(scheme_code: int):
    """NAV + computed returns + (best-effort) expense ratio for one scheme."""
    funds = await fetch_amfi(_client)
    fund = funds.get(scheme_code)
    if not fund:
        raise HTTPException(404, f"Scheme code {scheme_code} not found in AMFI feed.")

    # Fetch returns and expense ratio concurrently.
    returns, expense = await asyncio.gather(
        fetch_returns(_client, scheme_code),
        fetch_expense(_client, scheme_code),
    )

    return FundDetail(
        scheme_code=fund.scheme_code,
        scheme_name=fund.scheme_name,
        fund_house=fund.fund_house,
        scheme_category=fund.scheme_category,
        isin_growth=fund.isin_growth,
        nav=fund.nav,
        nav_date=fund.nav_date,
        expense_ratio=expense.get("expense_ratio"),
        expense_ratio_source=expense.get("expense_ratio_source"),
        rating=expense.get("rating"),
        returns=returns,
    )


@app.get("/funds/{scheme_code}/nav", tags=["funds"])
async def fund_nav(scheme_code: int):
    funds = await fetch_amfi(_client)
    fund = funds.get(scheme_code)
    if not fund:
        raise HTTPException(404, f"Scheme code {scheme_code} not found.")
    return {"scheme_code": scheme_code, "scheme_name": fund.scheme_name,
            "nav": fund.nav, "nav_date": fund.nav_date}


@app.get("/funds/{scheme_code}/returns", response_model=list[PeriodReturn], tags=["funds"])
async def fund_returns(scheme_code: int):
    funds = await fetch_amfi(_client)
    if scheme_code not in funds:
        raise HTTPException(404, f"Scheme code {scheme_code} not found.")
    return await fetch_returns(_client, scheme_code)
