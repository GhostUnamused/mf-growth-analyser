# MF Growth Analyser — Documentation

A Streamlit app that back-tests Indian mutual funds against a market index. Pick up to five funds, a benchmark, lumpsum or SIP investing and a period. The app shows how the money would have grown, how risky the ride was, what monthly SIP a target corpus needs, and recent news for each fund's theme.

---

## Running it

```bash
pip install -r requirements.txt
streamlit run app.py
```

Optional: put a [NewsAPI](https://newsapi.org) key in `.streamlit/secrets.toml` to source headlines from a curated list of financial outlets:

```toml
NEWSAPI_KEY = "your-key"
```

Without a key, headlines come from Google News RSS. `secrets.toml` is git-ignored. On Streamlit Community Cloud, add the key under *App settings → Secrets*.

The look of the app (colours, font, corner radius) is set in `.streamlit/config.toml`.

---

## Data sources

| Data | Source | Cache |
|---|---|---|
| Fund list and scheme codes | AMFI `NAVAll.txt` (names containing CLOSED, MATURED, SUSPENDED, IDCW or DIVIDEND are dropped) | 24 h |
| Daily NAV history | `api.mfapi.in/mf/{code}` | 1 h |
| Benchmark index | Yahoo Finance via `yfinance` | 1 h |
| News | NewsAPI if a key is configured, otherwise Google News RSS | 1 h |

| Benchmark | Yahoo ticker |
|---|---|
| Nifty 50 | `^NSEI` |
| BSE Sensex | `^BSESN` |
| Nifty 500 | `^CRSLDX` |
| Nifty Midcap 100 | `NIFTY_MIDCAP_100.NS` |

A failed download is not cached, so the next rerun tries again.

---

## How the numbers are calculated

### Simulation

Fund and index prices are merged on date, and missing days are forward-filled. Nothing is invested before an asset's first price: a fund launched partway through the period is simulated from its first NAV, and the chart says so.

- **Lumpsum**: the whole amount buys units on the first available date.
- **SIP**: one instalment on the first available date of each calendar month. With step-up, the instalment rises by the step-up % every 12 instalments.

### Returns

| Metric | Definition |
|---|---|
| Absolute return | `(value − invested) / invested` |
| XIRR (SIP) / CAGR (lumpsum) | Money-weighted annualised return, solved from the actual dated cash flows. For a lumpsum this equals CAGR. Shown only for periods of a year or more, following AMFI convention. |
| vs benchmark | Fund XIRR/CAGR minus the benchmark's, with the benchmark simulated over the **fund's own window** (same start date, same instalments). |

### Post-tax, inflation-adjusted mode

- **Tax**: LTCG at the chosen rate on gains above ₹1.25 L, as if the whole holding were redeemed on that date.
- **Inflation**: every amount, including each SIP instalment, is converted to rupees of the period's start date. Returns in this mode are therefore real returns.

### Risk (from each asset's own daily prices in the period)

| Metric | Definition |
|---|---|
| Price CAGR | Annualised growth of the NAV or index level |
| Volatility | Std. dev. of daily returns × √252 |
| Max drawdown | Largest fall from a previous peak |
| Best / worst 1Y | Range of returns over every rolling 12-month window (by calendar date) |
| Down years | Calendar years ending below the previous year-end |
| Years to double | ln 2 / ln(1 + CAGR) |

### Goal planner

Monthly SIP needed, from the future value of an annuity due at the period's price CAGR:

```
r   = (1 + CAGR)^(1/12) − 1
SIP = Goal × r / (((1 + r)^n − 1) × (1 + r))
```

When post-tax, inflation-adjusted mode is on, the goal is treated as today's rupees and grossed up by inflation over the horizon. Funds with a zero or negative CAGR are left blank.

### News

Each fund is given a theme (Mid Cap, Banking, Gilt, Technology…) by whole-word keyword matching on its name, and one query is made per theme.

---

## Limitations

1. **Price index vs TRI.** The benchmarks are price-return indices, but fund NAVs include reinvested dividends. Nifty 50 TRI has historically beaten the price index by roughly 1–1.5% a year, so funds look somewhat better than they are.
2. **Tax is simplified.** The ₹1.25 L exemption is applied once rather than per financial year. Short-term gains, per-instalment holding periods and debt-fund slab taxation are not modelled.
3. **SIP date.** Instalments go in on the first trading day of each month, not on a fixed mandate date.
4. **Drawdown is NAV-based.** It describes the fund, not your SIP portfolio.
5. **The planner is not a forecast.** It assumes the historical CAGR repeats.
6. **News is theme-based.** Holdings data is not freely available, so a diversified fund gets general market news.
7. **Third-party data.** `mfapi.in` is unofficial and `yfinance` scrapes Yahoo Finance; either can fail or rate-limit. The app shows an error and retries on the next run.
