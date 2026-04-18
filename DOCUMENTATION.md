# MF Growth Analyser — Documentation

---

## What This App Does

A Streamlit-based tool for analysing and simulating the historical performance of Indian Mutual Funds against benchmark indices. It lets you run Lumpsum or SIP simulations over any time window, compare multiple funds simultaneously, assess risk with data-driven metrics, and get contextual market news grouped by fund investment theme.

---

## Data Sources

### 1. Mutual Fund NAV — `mfapi.in`
- **URL:** `https://api.mfapi.in/mf/{scheme_code}`
- **What it provides:** Complete daily historical NAV for any AMFI-registered scheme, going back to fund inception.
- **Format:** JSON — `{ date: "DD-MM-YYYY", nav: "123.456" }`
- **Caching:** 1 hour (`st.cache_data(ttl=3600)`)
- **Note:** Only Growth plan NAVs are used. IDCW/Dividend variants are excluded at selection time (see Fund List below).

### 2. Active Fund List — AMFI India
- **URL:** `https://www.amfiindia.com/spages/NAVAll.txt`
- **What it provides:** The live official list of all AMFI-registered funds with their scheme codes and NAVs.
- **Filtering applied:** Fund names containing `CLOSED`, `MATURED`, `SUSPENDED`, `IDCW`, or `DIVIDEND` are excluded — the selector only shows active Growth-plan funds.
- **Caching:** 24 hours (`ttl=86400`)

### 3. Benchmark Index Data — Yahoo Finance via `yfinance`
- **Tickers used:**

| Label | Yahoo Finance Ticker | What it actually is |
|---|---|---|
| Nifty 50 | `^NSEI` | Nifty 50 Price Return Index |
| BSE Sensex | `^BSESN` | BSE 30 Price Return Index |
| Nifty 500 | `^CRSLDX` | NSE 500 Composite |
| Nifty Midcap 100 | `^NSEMDCP50` | **Nifty Midcap 50** *(see Limitations)* |

- **Caching:** 1 hour

### 4. Market News — NewsAPI
- **Endpoint:** `https://newsapi.org/v2/everything`
- **Filtering:** Results restricted to 10 trusted Indian/global financial domains: Economic Times, Livemint, Business Standard, Moneycontrol, The Hindu, Reuters, Business Today, Financial Express, NDTV Profit, Hindu BusinessLine.
- **Sorting:** `publishedAt` descending (newest first)
- **Caching:** 1 hour per theme query

---

## Features & Calculation Logic

### Fund Selector
- Separate search bar (uses a `text_input` widget) lets you filter the 5000+ fund list. The search box persists its text when you select a fund — Streamlit normally clears the multiselect search on every selection, which is why they are separate widgets.
- Selected funds are stored in `st.session_state` and always pre-pended to the options list, so they are never dropped when the filter changes.
- Hard cap: **max 5 funds** to prevent accidental data overload.

---

### Lumpsum Simulation

**How it works:**
1. Find the first available NAV on or after the selected start date.
2. Treat that as the base: `base_NAV = NAV[start_date]`
3. For every subsequent date: `portfolio_value = (NAV[date] / base_NAV) × investment_amount`

This is equivalent to buying units at `base_NAV` and tracking their value — which is exactly what a lumpsum investment does.

**Invested amount stays constant** throughout the simulation (no new money added). Only the value fluctuates.

---

### SIP Simulation

**How it works:**
1. Identify the **first available trading day of each calendar month** within the selected period.
2. On that day, calculate units bought: `units = round(SIP_amount / NAV[buy_date], 4)`
3. Track cumulative units and cumulative amount invested.
4. Portfolio value on any date: `cumulative_units × NAV[date]`

**Yearly Step-Up:**
- Every 12 months, increase the SIP amount: `SIP_month = base_SIP × (1 + step_up_pct/100)^years_elapsed`
- Applied prospectively from the anniversary date.

---

### Performance Metrics

**Annualized Return (CAGR):**
```
CAGR = (Final_Value / Total_Invested)^(1 / years) - 1
```
> ⚠️ See Limitations — for SIP, this is an approximation, not a true XIRR.

**Absolute Return:**
```
Abs_Return = (Final_Value - Total_Invested) / Total_Invested × 100
```

**Outperformance:**
```
Outperformance = Fund_CAGR - Benchmark_CAGR
```

---

### Tax & Inflation Adjustment

When enabled, the portfolio value is adjusted in two steps applied at the end of the simulation:

**Step 1 — LTCG Tax:**
```
Gross Profit     = Final_Value - Total_Invested
Taxable Profit   = max(0, Gross_Profit - ₹1,25,000)   ← LTCG exemption
Tax              = Taxable_Profit × (tax_rate / 100)
Post-Tax Value   = Final_Value - Tax
```

**Step 2 — Inflation Deflation:**
```
Real_Value = Post_Tax_Value / (1 + inflation_rate/100)^years
```
> ⚠️ See Limitations for caveats on this calculation.

---

### Target Wealth Planner (Reverse SIP)

Given a **Goal Amount (₹)** and a **Time Horizon (years)**, calculates the monthly SIP required to reach that goal — using the **CAGR from the currently selected timeframe** as the assumed growth rate.

**Monthly rate from CAGR:**
```
r_monthly = (1 + CAGR/100)^(1/12) - 1
```

**Required PMT (annuity-due formula — payment at start of each period):**
```
PMT = (Goal × r_monthly) / ((1 + r_monthly)^n_months - 1) × (1 + r_monthly)
```
> This is the standard **future value of an annuity due** formula.

If CAGR ≤ 0 (flat or declining fund), the planner falls back to simple division: `Goal / n_months`.

The planner table updates dynamically as you change the timeframe — selecting 1Y vs 5Y will use that period's actual CAGR.

---

### Risk & Consistency Metrics

All metrics are computed over the **selected timeframe** using the forward-filled daily NAV series (`sim_df`).

| Metric | Formula / Method |
|---|---|
| **Best 1-Year Return** | `max(rolling 252-day % change)` |
| **Worst 1-Year Return** | `min(rolling 252-day % change)` |
| **Max Drawdown** | `min((NAV - running_peak) / running_peak × 100)` |
| **Annualised Volatility** | `std(daily returns) × √252 × 100` |
| **Loss-Making Years** | Count of calendar years where year-end NAV < prior year-end NAV |
| **Years to Double** | `72 / CAGR` (Rule of 72) |

**252** = approximate number of trading days in a year on NSE/BSE.

---

### Market News

Funds are classified into a theme (e.g., "Banking & PSU Debt", "Nifty 50 / Large Cap Index") by matching keywords in the fund name against a priority-ordered lookup table. Funds sharing the same theme are grouped, and a **single NewsAPI query** is made per unique theme — avoiding redundant API calls and hitting rate limits.

Results are cached for 1 hour, so re-selecting the same fund type within an hour uses the cached response.

---

## Real Limitations

### Data & Methodology

**1. SIP Annualized Return is not XIRR**
The CAGR shown for SIP treats the total amount invested as if it were a single lumpsum at the start date. The mathematically correct metric is **XIRR** (Extended Internal Rate of Return), which accounts for the fact that each monthly instalment is invested at a different date. The current method can overstate CAGR significantly when a fund has had a recent surge, because it doesn't weight early instalments as less impactful.

**2. Price Return Index vs Total Return Index (TRI)**
Yahoo Finance provides MF NAVs from the **AMFI data**, which are Growth plan NAVs — these already include reinvested dividends. However, the benchmark indices (`^NSEI`, `^BSESN`) are **Price Return indices**, which do NOT include dividend reinvestment. The Nifty 50 TRI has historically outperformed the PR index by approximately **1.5–2% per year**. This means benchmarks appear weaker than they actually are, making funds look better by comparison.

**3. Wrong ticker for Nifty Midcap 100**
The benchmark labelled "Nifty Midcap 100" uses Yahoo Finance ticker `^NSEMDCP50`, which is actually the **Nifty Midcap 50** index, not the Nifty Midcap 100. Comparison against this benchmark is comparing against the wrong index.

**4. Tax calculation is simplified**
- The ₹1,25,000 LTCG exemption is applied to the **total profit as a lump sum**, not annually as it works in practice.
- Short-term capital gains (STCG, holdings under 1 year) are not separated — the same LTCG rate applies to the entire profit regardless of holding period.
- Does not account for debt fund taxation (now taxed at income slab rate after April 2023 budget changes).
- Tax is computed at the **end of simulation**, not on a rolling/year-wise basis.

**5. Max Drawdown is NAV-based, not portfolio-based**
For a SIP simulation, your actual portfolio drawdown differs from the NAV's drawdown — because you were buying units at various prices throughout. The max drawdown shown reflects the **underlying fund's worst NAV drop**, not what your specific SIP portfolio would have experienced.

**6. Forward-fill for missing trading days**
Weekends, holidays, and days without NAV data are filled using the previous day's NAV (`ffill()`). This is technically correct (last known price), but it artificially suppresses volatility calculations for funds with frequent data gaps (e.g., newly launched funds, or funds that missed reporting NAV for a few days).

**7. SIP executed on first trading day of month**
Real SIP mandates are executed on a fixed calendar date (such as the 1st or 10th of each month, regardless of whether it's a trading day). This app uses the first available NAV date of each calendar month, which can differ by up to a few days and introduces a small systematic timing difference.

**8. CAGR used in Wealth Planner is not a forecast**
The planner uses the selected period's historical CAGR as the growth assumption. This is explicitly labelled, but it bears emphasis: past CAGR does not predict future returns. A fund with 18% CAGR over 5Y may deliver 6% or 30% in the next 5 years. The planner gives a reference calculation, not a financial plan.

### News

**9. News is theme-based, not holdings-based**
The app does not have access to a fund's actual portfolio holdings (the top 10 stocks, sector breakdown etc.) — this data is not freely available in a structured API form for Indian MFs. News is instead fetched for the fund's broad investment theme (e.g., "Banking & PSU bonds" or "Nifty 50 index"). For a diversified equity fund, this means news is generic Indian equity market news, not specific to the fund's actual bets.

**10. NewsAPI free tier — 100 requests/day**
With 1-hour caching and theme deduplication, normal use stays well within this limit. However, if the Streamlit app is shared publicly or heavily tested, this ceiling can be hit.

### Infrastructure

**11. No user authentication or data persistence**
All state is in-memory within a single Streamlit session. Refreshing the page resets selections. There is no saved portfolio, no historical comparison logs, and no user accounts.

**12. mfapi.in is an unofficial free API**
The NAV data is reliable and sourced ultimately from AMFI, but `mfapi.in` is not an official AMFI service. If the service goes down or rate-limits aggressively, fund data will fail to load. AMFI itself does not provide a NAV history API.

**13. Yahoo Finance availability**
`yfinance` is a scraper-based library and can fail intermittently if Yahoo Finance changes its API or applies rate limits. Index data is the most likely point of failure in the app.
