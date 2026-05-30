# Indian Mutual Funds API

A small, self-hostable REST API serving **latest NAV**, **returns**, and a
**best-effort expense ratio** for (almost) every mutual fund in India.

## Run it

```bash
pip install -r requirements.txt
uvicorn main:app --reload
```

Then open the interactive docs: **http://127.0.0.1:8000/docs**

## Endpoints

| Endpoint | What it returns |
|---|---|
| `GET /funds` | All funds + latest NAV. Filters: `search`, `fund_house`, `category`, `page`, `page_size` |
| `GET /funds/{scheme_code}` | NAV + returns (1m–5y) + expense ratio + rating |
| `GET /funds/{scheme_code}/nav` | Latest NAV only |
| `GET /funds/{scheme_code}/returns` | Computed returns only |
| `GET /health` | Service + AMFI source health |

Example:
```bash
curl "http://127.0.0.1:8000/funds?search=parag%20parikh%20flexi&page_size=5"
curl "http://127.0.0.1:8000/funds/122640"   # detail with returns + expense ratio
```

## Where the data comes from

| Field | Source | Notes |
|---|---|---|
| Fund universe + latest NAV | **AMFI** `NAVAll.txt` | Official, free, one request covers ~12,000 schemes |
| Returns (1m–5y, absolute + CAGR) | computed from **mfapi.in** NAV history | Point-to-point; computed locally so the numbers are transparent |
| Expense ratio + rating | **mfdata.in** | Community source; **best-effort**, may be missing/stale for some schemes |

## Important caveats

- **Expense ratio is the weak link.** India has no single clean official API for it.
  The authoritative source is each AMC's monthly TER disclosure (and AMFI's TER page).
  Treat the value here as indicative, not official.
- **Returns** are point-to-point CAGR/absolute from Growth-plan NAV. Factsheet numbers
  may differ slightly (rolling returns, exact dates, IDCW handling).
- Listing **all** funds returns NAV only. Returns + expense ratio are fetched per-scheme
  (on the detail endpoint) to stay fast and polite to upstream sources. To enrich all
  funds you'd batch the detail calls yourself, respecting rate limits, and cache results.
- This wraps free community/official feeds. **Accuracy is not guaranteed and this is
  not investment advice.** For production/commercial use, consider a licensed data
  provider (e.g. a paid market-data vendor).

## Going to production

- Swap the in-memory cache for Redis.
- Add a nightly job that downloads AMFI once and pre-computes returns for the whole
  universe into your own DB, so `/funds` can serve enriched data instantly.
- Add API-key auth + rate limiting if exposing publicly.
