import re
import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import requests
import datetime
import yfinance as yf
from duckduckgo_search import DDGS


def _fmt_inr(value) -> str:
    """Format a number with Indian comma grouping (e.g. 12,34,567 = 12.34 Lakhs)."""
    try:
        v = int(round(float(value)))
    except (TypeError, ValueError):
        return str(value)
    negative = v < 0
    v = abs(v)
    s = str(v)
    if len(s) <= 3:
        grouped = s
    else:
        grouped = s[-3:]
        s = s[:-3]
        while s:
            grouped = s[-2:] + ',' + grouped
            s = s[:-2]
    return f"₹ {'-' if negative else ''}{grouped}"


def _short_name(name: str, max_len: int = 35) -> str:
    """Return a concise legend label, preserving plan type (D/R) for disambiguation."""
    q = name.upper()
    # Remove truly useless parenthetical (old brand name)
    q = re.sub(r'\(FORMERLY KNOWN AS[^)]*\)', '', q)
    # Remove verbose IDCW / payout description
    q = re.sub(r'INCOME DISTRIBUTION CUM CAPITAL WITHDRAWAL OPTION.*', '', q)
    q = re.sub(r'\(PAYOUT\s*&?\s*REINVESTMENT\)', '', q, flags=re.IGNORECASE)
    q = re.sub(r'\b(REINVESTMENT|PAYOUT)\b', '', q)
    # Compact plan type — keep as short suffix so Direct vs Regular stays visible
    q = re.sub(r'-?\s*DIRECT PLAN\b', ' (D)', q)
    q = re.sub(r'-?\s*REGULAR PLAN\b', ' (R)', q)
    # Strip frequency and redundant option keywords
    q = re.sub(
        r'-?\s*\b(FORTNIGHTLY|MONTHLY|QUARTERLY|ANNUAL|DAILY|WEEKLY|'
        r'GROWTH OPTION|GROWTH|IDCW|BONUS|OPTION)\b', '', q
    )
    q = re.sub(r'[\s-]+$', '', q.strip())
    q = re.sub(r'\s+', ' ', q).strip(' -')
    return q if len(q) <= max_len else q[:max_len].rstrip() + '…'

st.set_page_config(page_title="MF Growth Analyser", layout="wide")

st.title("MF Growth Analyser")

planner_expander = st.expander("Target Wealth Planner (Reverse SIP)")
with planner_expander:
    col_g, col_h = st.columns(2)
    goal_amt = col_g.number_input("Financial Goal (₹)", min_value=10000, value=10000000, step=100000)
    horizon_yrs = col_h.number_input("Time Horizon (Years)", min_value=1, value=10, step=1)
    planner_results_placeholder = st.empty()

# --- Constants ---
BENCHMARKS = {
    "Nifty 50": "^NSEI", 
    "BSE Sensex": "^BSESN", 
    "Nifty 500": "^CRSLDX", 
    "Nifty Midcap 100": "^NSEMDCP50"
}
EXCLUDE_WORDS = ['CLOSED', 'MATURED', 'SUSPENDED', 'IDCW', 'DIVIDEND']

@st.cache_data(ttl=86400)
def fetch_active_funds():
    try:
        res = requests.get('https://www.amfiindia.com/spages/NAVAll.txt', timeout=15)
        res.raise_for_status()
        lines = res.text.split('\n')
        fund_dict = {}
        for line in lines:
            parts = line.split(';')
            if len(parts) >= 6 and parts[0].strip().isdigit():
                code = parts[0].strip()
                name = parts[3].strip().upper()
                if not any(word in name for word in EXCLUDE_WORDS):
                    fund_dict[name] = code
        return fund_dict
    except Exception:
        return {}

@st.cache_data(ttl=3600)
def fetch_mf_data(scheme_code):
    try:
        res = requests.get(f"https://api.mfapi.in/mf/{scheme_code}")
        res.raise_for_status()
        data = res.json()
        if "data" not in data or not data["data"]:
            return None
        df = pd.DataFrame(data["data"])
        df["Date"] = pd.to_datetime(df["date"], format="%d-%m-%Y")
        df["MF_NAV"] = pd.to_numeric(df["nav"], errors="coerce")
        df = df.dropna(subset=['MF_NAV']).sort_values("Date").reset_index(drop=True)
        return df[["Date", "MF_NAV"]]
    except Exception:
        return None

@st.cache_data(ttl=3600)
def fetch_index_data(ticker, start_dt="2000-01-01"):
    try:
        df = yf.download(ticker, start=start_dt)
        if df.empty:
            return None
        df = df.reset_index()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df[['Date', 'Close']].copy()
        
        if 'Close' in df.columns and isinstance(df['Close'], pd.DataFrame):
            df['Close'] = df['Close'].iloc[:, 0]
            
        df = df.rename(columns={'Close': 'Index_Close'})
        df['Date'] = pd.to_datetime(df['Date']).dt.tz_localize(None)
        return df.sort_values('Date').reset_index(drop=True)
    except Exception:
        return None

# --- Master Controls (No Sidebar) ---
col1, col2, col3, col4, col5 = st.columns(5)

with col1:
    selected_benchmark_name = st.selectbox("Select Benchmark Index", options=list(BENCHMARKS.keys()), index=3)
    benchmark_ticker = BENCHMARKS[selected_benchmark_name]

with col2:
    investment_type = st.radio("Investment Type", options=["Lumpsum", "SIP"])

with col3:
    label = "Monthly SIP Amount (₹)" if investment_type == "SIP" else "Initial Lumpsum (₹)"
    amount = st.number_input(label, min_value=500, value=10000, step=500)

with col4:
    if investment_type == "SIP":
        step_up_pct = st.slider("Yearly Step-Up (%)", min_value=0, max_value=50, value=0, step=1)
    else:
        step_up_pct = 0

with col5:
    apply_taxes_inflation = st.toggle("Adjust for Taxes & Inflation")
    if apply_taxes_inflation:
        sub_col1, sub_col2 = st.columns(2)
        with sub_col1:
            tax_rate = st.number_input("LTCG Tax (%)", value=12.5, step=0.5)
        with sub_col2:
            inflation_rate = st.number_input("Inflation (%)", value=5.5, step=0.5)

all_funds = fetch_active_funds()
if not all_funds:
    st.error("Failed to load active funds database.")
    st.stop()

# Fund search — separate text input so the search term persists across fund selections
fund_search = st.text_input(
    "Search Funds",
    placeholder="Type AMC name, category, keyword…",
    key="fund_search_input"
)
if fund_search:
    filtered_options = [n for n in all_funds.keys() if fund_search.upper() in n]
else:
    filtered_options = list(all_funds.keys())

# Always keep any already-selected funds in the options list so they are never
# silently dropped when the search filter changes (Streamlit drops values that
# are not present in `options` on rerun).
_current_selection = st.session_state.get("fund_multiselect", [])
_merged_options = list(dict.fromkeys(_current_selection + filtered_options))

selected_fund_names = st.multiselect(
    "Select Mutual Funds (max 5)",
    options=_merged_options,
    max_selections=5,
    placeholder="Select up to 5 funds from the filtered list…",
    key="fund_multiselect"
)

if not selected_fund_names:
    st.info("Please select at least one mutual fund.")
    st.stop()


# --- Data Fetching & Time Sync ---
with st.spinner("Fetching live portfolio data..."):
    index_df = fetch_index_data(benchmark_ticker)
    if index_df is None:
        st.error("Failed to fetch Index data.")
        st.stop()

    mf_dfs = {}
    for fname in selected_fund_names:
        scode = all_funds[fname]
        df = fetch_mf_data(scode)
        if df is not None and not df.empty:
            df = df.rename(columns={"MF_NAV": fname})
            mf_dfs[fname] = df

    if not mf_dfs:
        st.error("Failed to fetch any Mutual Fund data. They may be inactive.")
        st.stop()

    merged_df = index_df.copy()
    for fname, df in mf_dfs.items():
        merged_df = pd.merge(merged_df, df, on="Date", how="outer")

    merged_df = merged_df.sort_values("Date")

    valid_dates_all = merged_df.dropna(subset=['Index_Close'] + list(mf_dfs.keys()), how='all')["Date"]
    if valid_dates_all.empty:
        st.warning("No overlapping data found for the selected assets.")
        st.stop()

    min_date = valid_dates_all.min().date()
    max_date = merged_df["Date"].max().date()


# Planner calculation is deferred below — after filtered_df is built from the selected timeframe


st.write("---")
# --- Hybrid Time UI ---
time_choice = st.radio("Timeframe", ['1M', '6M', 'YTD', '1Y', '3Y', '5Y', '10Y', 'Max', 'Custom'], horizontal=True, index=4)

max_dt_ts = pd.to_datetime(max_date)

if time_choice == '1M':
    start_dt = max_dt_ts - pd.DateOffset(months=1)
elif time_choice == '6M':
    start_dt = max_dt_ts - pd.DateOffset(months=6)
elif time_choice == 'YTD':
    start_dt = pd.to_datetime(datetime.date(max_dt_ts.year, 1, 1))
elif time_choice == '1Y':
    start_dt = max_dt_ts - pd.DateOffset(years=1)
elif time_choice == '3Y':
    start_dt = max_dt_ts - pd.DateOffset(years=3)
elif time_choice == '5Y':
    start_dt = max_dt_ts - pd.DateOffset(years=5)
elif time_choice == '10Y':
    start_dt = max_dt_ts - pd.DateOffset(years=10)
elif time_choice == 'Max':
    start_dt = pd.to_datetime(min_date)
else:
    start_dt = pd.to_datetime(min_date)

start_dt = max(start_dt, pd.to_datetime(min_date))
end_dt = max_dt_ts

if time_choice == 'Custom':
    selected_dates = st.slider(
        "Select Custom Date Range",
        min_value=min_date,
        max_value=max_date,
        value=(start_dt.date(), end_dt.date())
    )
    start_dt = pd.to_datetime(selected_dates[0])
    end_dt = pd.to_datetime(selected_dates[1])


# --- Strict Nearest Valid Trading Day ---
valid_start_dates = merged_df[merged_df["Date"] >= start_dt]["Date"]
if not valid_start_dates.empty:
    start_dt = valid_start_dates.iloc[0]

# Preserve unadulterated NaN states locally for precision checks
filtered_df = merged_df[(merged_df["Date"] >= start_dt) & (merged_df["Date"] <= end_dt)].copy().reset_index(drop=True)

if filtered_df.empty:
    st.warning("No valid trading days found in the selected date range.")
    st.stop()
    
# Render cleanly filled subsets structurally strictly for mathematical plotting boundaries
sim_df = filtered_df.ffill().bfill()


# --- Target Wealth Planner (uses selected timeframe CAGR, not all-time max) ---
planner_data = []
n_months = horizon_yrs * 12
period_label = time_choice if time_choice != 'Custom' else "Custom Range"

for asset_name in list(mf_dfs.keys()) + ["Index_Close"]:
    prices_df = filtered_df[['Date', asset_name]].dropna()
    if len(prices_df) > 2:
        start_val = prices_df[asset_name].iloc[0]
        end_val = prices_df[asset_name].iloc[-1]
        yrs = (prices_df['Date'].iloc[-1] - prices_df['Date'].iloc[0]).days / 365.25

        c_ret = 0.0
        if yrs > 0 and start_val > 0:
            c_ret = ((end_val / start_val) ** (1 / yrs) - 1) * 100

        r_month = ((1 + c_ret / 100.0) ** (1 / 12.0)) - 1
        if r_month > 0:
            pmt = (goal_amt * r_month) / (((1 + r_month) ** n_months - 1) * (1 + r_month))
        else:
            pmt = goal_amt / n_months

        display_name = selected_benchmark_name if asset_name == "Index_Close" else asset_name
        planner_data.append({
            "Asset / Benchmark": display_name,
            f"CAGR ({period_label})": f"{c_ret:.2f}%",
            "Required Monthly SIP": _fmt_inr(pmt),
        })

with planner_results_placeholder.container():
    st.caption(f"SIP required to reach your goal, calculated using **{period_label}** period returns. Change the timeframe above to update.")
    st.dataframe(pd.DataFrame(planner_data), use_container_width=True, hide_index=True)


# --- Core Math Simulation ---
# Draw plotting indices entirely dynamically 
result_df = sim_df[['Date']].copy()
total_invested_df = sim_df[['Date']].copy()
assets = ['Index_Close'] + list(mf_dfs.keys())

if investment_type == "Lumpsum":
    for asset in assets:
        total_invested_df[asset] = amount
        # Base value relies on actual existing trace boundaries natively
        valid_history = filtered_df[asset].dropna()
        base_val = valid_history.iloc[0] if not valid_history.empty else sim_df[asset].iloc[0]
        
        if pd.isna(base_val) or base_val == 0:
            result_df[asset] = None
        else:
            result_df[asset] = (sim_df[asset] / base_val) * amount

elif investment_type == "SIP":
    sim_df['YearMonth'] = sim_df['Date'].dt.to_period('M')
    buy_dates_idx = sim_df.groupby('YearMonth')['Date'].idxmin()
    
    for asset in assets:
        units_bought = np.zeros(len(sim_df))
        invested_tracking = np.zeros(len(sim_df))
        asset_prices = sim_df[asset].values
        
        month_count = 0
        for i, b_idx in enumerate(buy_dates_idx.index):
            row_idx = buy_dates_idx[b_idx]
            years_passed = month_count // 12
            current_sip_amount = amount * ((1 + step_up_pct / 100.0) ** years_passed)
            
            price = asset_prices[row_idx]
            if not np.isnan(price) and price > 0:
                units_bought[row_idx] = np.round(current_sip_amount / price, 4)
            invested_tracking[row_idx] = current_sip_amount
            month_count += 1
            
        cumulative_units = np.cumsum(units_bought)
        cumulative_invested = np.cumsum(invested_tracking)
        
        result_df[asset] = cumulative_units * asset_prices
        total_invested_df[asset] = cumulative_invested

# --- Tax / Inflation Adjustments ---
if apply_taxes_inflation:
    date_diff_years = (result_df['Date'] - result_df['Date'].iloc[0]).dt.days / 365.25
    date_diff_years = date_diff_years.replace(0, 0.0001)
    
    for asset in assets:
        gross_profit = result_df[asset] - total_invested_df[asset]
        taxable_profit = (gross_profit - 125000).clip(lower=0)
        tax_amount = taxable_profit * (tax_rate / 100.0)
        
        post_tax_value = result_df[asset] - tax_amount
        real_value = post_tax_value / ((1 + (inflation_rate / 100.0)) ** date_diff_years)
        result_df[asset] = real_value


# --- Plotly color palette (mirrors Plotly's default sequence) ---
_PLOTLY_PALETTE = [
    '#636EFA', '#EF553B', '#00CC96', '#AB63FA', '#FFA15A',
    '#19D3F3', '#FF6692', '#B6E880', '#FF97FF', '#FECB52'
]

# Build unique legend names: deduplicate after boilerplate stripping
from collections import Counter as _Counter
_raw_shorts = {f: _short_name(f) for f in mf_dfs.keys()}
_dup_counts = _Counter(_raw_shorts.values())
_dup_idx: dict = {}
LEGEND_NAMES: dict = {}
for _f, _s in _raw_shorts.items():
    if _dup_counts[_s] > 1:
        _dup_idx[_s] = _dup_idx.get(_s, 0) + 1
        LEGEND_NAMES[_f] = f"{_s} ({_dup_idx[_s]})"
    else:
        LEGEND_NAMES[_f] = _s

# Assign explicit colors per asset so chart ↔ table match perfectly
ASSET_COLORS: dict = {selected_benchmark_name: _PLOTLY_PALETTE[0]}
for _i, _f in enumerate(mf_dfs.keys(), start=1):
    ASSET_COLORS[_f] = _PLOTLY_PALETTE[_i % len(_PLOTLY_PALETTE)]

# --- Universal Callbacks and Data Prep ---
tab_perf, tab_risk, tab_news = st.tabs(["Performance", "Risk & Consistency", "Market News"])

def calc_return(invested, final_val, yrs):
    if yrs <= 0 or invested <= 0:
        return 0.0
    return ((final_val / invested) ** (1/yrs) - 1) * 100

def calc_abs_return(invested, final_val):
    if invested <= 0:
        return 0.0
    return ((final_val - invested) / invested) * 100

def color_formatting(val):
    if pd.isna(val) or val == "-":
        return ''
    try:
        if float(val) > 0:
            return 'color: #00FF00;'
        elif float(val) < 0:
            return 'color: #FF0000;'
    except:
        pass
    return 'color: gray;'

def get_true_metrics(asset_col):
    """ Extract exact metrics securely bypassing global forward-fill extrapolation bounds natively """
    valid_series = filtered_df[asset_col].dropna()
    if valid_series.empty:
        return 0.0, 0.0, 0.0001
        
    last_valid_idx = valid_series.index[-1]
    first_valid_idx = valid_series.index[0]
    
    final_val = result_df[asset_col].loc[last_valid_idx]
    invest_val = total_invested_df[asset_col].loc[last_valid_idx]
    
    true_elapsed = (filtered_df['Date'].loc[last_valid_idx] - filtered_df['Date'].loc[first_valid_idx]).days / 365.25
    if true_elapsed <= 0:
        true_elapsed = 0.0001
        
    return final_val, invest_val, true_elapsed

benchmark_final, benchmark_invested, bm_years = get_true_metrics('Index_Close')
benchmark_ret = calc_return(benchmark_invested, benchmark_final, bm_years) if benchmark_invested > 0 else 0.0
benchmark_abs_ret = calc_abs_return(benchmark_invested, benchmark_final) if benchmark_invested > 0 else 0.0

bm_metrics_data = [{
    "Asset Name": selected_benchmark_name,
    "Total Invested (₹)": benchmark_invested,
    "Final Value (₹)": benchmark_final,
    "Absolute Return (%)": benchmark_abs_ret,
    "Annualized Return (%)": benchmark_ret,
    "Outperformance (%)": np.nan
}]

fund_metrics_data = []

for fname in mf_dfs.keys():
    fund_final, fund_invested, fund_years = get_true_metrics(fname)
    
    if fund_final == 0:
        continue
        
    fund_ret = calc_return(fund_invested, fund_final, fund_years)
    fund_abs_ret = calc_abs_return(fund_invested, fund_final)
    outperf = fund_ret - benchmark_ret
    
    fund_metrics_data.append({
        "Asset Name": fname,
        "Total Invested (₹)": fund_invested,
        "Final Value (₹)": fund_final,
        "Absolute Return (%)": fund_abs_ret,
        "Annualized Return (%)": fund_ret,
        "Outperformance (%)": outperf
    })

# --- TAB 1: PERFORMANCE ---
with tab_perf:
    st.subheader("Performance Scorecards")
    cols = st.columns(len(assets))
    with cols[0]:
        st.metric(
            label=f"{selected_benchmark_name} (Benchmark)", 
            value=_fmt_inr(benchmark_final), 
            delta=f"{benchmark_abs_ret:.2f}%"
        )
        
    for idx, fname in enumerate(mf_dfs.keys(), start=1):
        f_final, f_inv, f_yrs = get_true_metrics(fname)
        f_abs_ret = calc_abs_return(f_inv, f_final)
        
        if idx < len(cols):
            with cols[idx]:
                st.metric(
                    label=fname, 
                    value=_fmt_inr(f_final) if f_final > 0 else "-", 
                    delta=f"{f_abs_ret:.2f}%" if f_final > 0 else "-"
                )

    st.write("---")
    
    # Plotly Canvas
    fig = go.Figure()

    fig.add_trace(go.Scatter(
        x=result_df['Date'],
        y=result_df['Index_Close'],
        mode='lines',
        name=selected_benchmark_name,
        line=dict(width=2, color=ASSET_COLORS[selected_benchmark_name]),
        hovertemplate="<b>" + selected_benchmark_name + "</b><br>Value: ₹ %{y:,.0f}<extra></extra>"
    ))

    for fname in mf_dfs.keys():
        n_rows = len(result_df)
        fig.add_trace(go.Scatter(
            x=result_df['Date'],
            y=result_df[fname],
            mode='lines',
            name=LEGEND_NAMES[fname],
            line=dict(color=ASSET_COLORS[fname]),
            customdata=[fname] * n_rows,
            hovertemplate="<b>%{customdata}</b><br>Value: ₹ %{y:,.0f}<extra></extra>"
        ))

    fig.update_layout(
        title=f"Portfolio Growth via {investment_type} (From {start_dt.strftime('%b %d, %Y')} to {end_dt.strftime('%b %d, %Y')})",
        hovermode="x unified",
        yaxis=dict(title="Portfolio Value (₹)", tickformat=","),
        xaxis=dict(title=""),
        margin=dict(l=0, r=0, t=50, b=0)
    )
    st.plotly_chart(fig, use_container_width=True)

    # Core Metric Tables
    format_dict = {
        "Total Invested (₹)": _fmt_inr,
        "Final Value (₹)": _fmt_inr,
        "Absolute Return (%)": "{:.2f}%", 
        "Annualized Return (%)": "{:.2f}%", 
        "Outperformance (%)": lambda x: f"{x:.2f}%" if pd.notna(x) else "-"
    }
    
    def _style_asset_col(series):
        """Color each Asset Name cell to match its chart trace colour."""
        return [
            f'color: {ASSET_COLORS.get(v, "inherit")}; font-weight: 600'
            for v in series
        ]

    bm_df = pd.DataFrame(bm_metrics_data)
    fund_df = pd.DataFrame(fund_metrics_data)

    st.subheader("Benchmark Index")
    st.dataframe(
        bm_df.style
            .format(format_dict)
            .apply(_style_asset_col, subset=["Asset Name"]),
        use_container_width=True, hide_index=True
    )

    st.subheader("Mutual Funds")
    if not fund_df.empty:
        styled_fund_df = (
            fund_df.style
                .format(format_dict)
                .apply(_style_asset_col, subset=["Asset Name"])
                .map(color_formatting, subset=["Absolute Return (%)", "Annualized Return (%)", "Outperformance (%)"])
        )
        st.dataframe(styled_fund_df, use_container_width=True, hide_index=True)

    export_df = pd.concat([bm_df, fund_df], ignore_index=True)
    csv_data = export_df.to_csv(index=False)
    st.download_button(
        label="Download Analysis Report (CSV)", 
        data=csv_data, 
        file_name='MF_Analysis.csv', 
        mime='text/csv'
    )


# --- TAB 2: RISK & CONSISTENCY ---
with tab_risk:
    st.subheader("Risk & Consistency Analytics")
    st.markdown("""
    **Metrics Overview:**
    - **Best / Worst 1-Year Return:** Highest and lowest return across any rolling 12-month window in the selected period.
    - **Max Drawdown:** Largest peak-to-trough NAV decline — the worst loss you could have suffered buying at a local high.
    - **Annualised Volatility:** Standard deviation of daily returns scaled to a year. Lower = smoother ride.
    - **Loss-Making Years:** Count of calendar years the fund closed in the red.
    - **Years to Double:** Rule-of-72 estimate using the selected-period CAGR.
    """)

    bm_risk_data = []
    fund_risk_data = []

    for asset in assets:
        is_index = (asset == 'Index_Close')
        asset_label = selected_benchmark_name if is_index else asset

        asset_prices = sim_df[asset].dropna()
        if asset_prices.empty:
            continue

        total_days = len(asset_prices)

        # Rolling 1-year best / worst
        if total_days > 252:
            rolling_1y = asset_prices.pct_change(periods=252).dropna() * 100
            best_1y  = rolling_1y.max()
            worst_1y = rolling_1y.min()
        else:
            best_1y = worst_1y = np.nan

        # Max drawdown: largest peak-to-trough percentage decline
        cummax       = asset_prices.expanding().max()
        drawdown_pct = (asset_prices - cummax) / cummax * 100
        max_drawdown = drawdown_pct.min()

        # Annualised volatility (std of daily returns × √252)
        daily_rets = asset_prices.pct_change().dropna()
        ann_vol    = daily_rets.std() * np.sqrt(252) * 100 if len(daily_rets) > 1 else np.nan

        # Calendar-year loss count
        prices_with_dates = sim_df.set_index('Date')[asset].dropna()
        yearly_prices     = prices_with_dates.resample('YE').last()
        yearly_returns    = yearly_prices.pct_change() * 100
        loss_making_years = int((yearly_returns < 0).sum())

        # Years to double via Rule of 72 (uses selected-period CAGR)
        end_val, invest_val, asset_years = get_true_metrics(asset)
        asset_cagr     = calc_return(invest_val, end_val, asset_years)
        years_to_double = (72 / asset_cagr) if asset_cagr > 0 else np.nan

        risk_row = {
            "Asset Name":                asset_label,
            "Best 1-Year Return (%)": best_1y,
            "Worst 1-Year Return (%)": worst_1y,
            "Max Drawdown (%)": max_drawdown,
            "Annualised Volatility (%)": ann_vol,
            "Loss-Making Years":         loss_making_years,
            "Years to Double":           years_to_double,
        }

        if is_index:
            bm_risk_data.append(risk_row)
        else:
            fund_risk_data.append(risk_row)

    risk_format_dict = {
        "Best 1-Year Return (%)": "{:.2f}%",
        "Worst 1-Year Return (%)": "{:.2f}%",
        "Max Drawdown (%)": "{:.2f}%",
        "Annualised Volatility (%)": "{:.2f}%",
        "Years to Double": "{:.1f} yrs",
    }
    _risk_color_cols = ["Best 1-Year Return (%)", "Worst 1-Year Return (%)", "Max Drawdown (%)"]

    st.subheader("Benchmark Index")
    st.dataframe(
        pd.DataFrame(bm_risk_data).style
            .format(risk_format_dict, na_rep="-")
            .apply(_style_asset_col, subset=["Asset Name"]),
        use_container_width=True, hide_index=True
    )

    st.subheader("Mutual Funds")
    if fund_risk_data:
        fund_risk_df = pd.DataFrame(fund_risk_data)
        st.dataframe(
            fund_risk_df.style
                .format(risk_format_dict, na_rep="-")
                .apply(_style_asset_col, subset=["Asset Name"])
                .map(color_formatting, subset=_risk_color_cols),
            use_container_width=True,
            hide_index=True
        )


# --- TAB 3: MARKET NEWS ---

# Maps keywords found in fund names → (Google News search query, human-readable theme label)
# Ordered by specificity (more specific patterns first)
_THEME_MAP = [
    (['NIFTY 50',  'NIFTY50',  'NIFTY NEXT 50'],        'Nifty 50 NSE index India stocks',              'Nifty 50 / Large Cap Index'),
    (['NIFTY MIDCAP', 'MIDCAP', 'MID CAP'],             'NSE midcap India equity stocks market',         'Mid Cap Equity'),
    (['SMALL CAP', 'SMALLCAP'],                          'NSE smallcap India equity stocks market',       'Small Cap Equity'),
    (['SENSEX', 'BSE 500', 'BSE 200'],                   'BSE Sensex India stocks market',                'BSE Index'),
    (['BANKING & PSU', 'BANKING AND PSU', 'PSU DEBT'],   'Indian banking PSU bonds RBI debt market',      'Banking & PSU Debt'),
    (['BANKING', 'BANK', 'FINANCIAL SERVICES'],          'Indian banking sector stocks NSE BSE',          'Banking & Financial Services'),
    (['CORPORATE BOND', 'CREDIT RISK'],                  'India corporate bonds credit market yield',     'Corporate Bonds'),
    (['OVERNIGHT', 'LIQUID'],                            'RBI repo rate India money market overnight',    'Money Market / Overnight'),
    (['GILT', 'GSEC', 'G-SEC', 'GOVERNMENT SECURITIES'], 'RBI India government securities gilt bonds',   'Government Securities / Gilt'),
    (['IT', 'TECHNOLOGY', 'TECH'],                       'India IT technology sector stocks Infosys TCS', 'Technology Sector'),
    (['PHARMA', 'HEALTHCARE', 'HEALTH CARE'],             'India pharma healthcare stocks market NSE',     'Pharma & Healthcare'),
    (['INFRASTRUCTURE', 'INFRA'],                        'India infrastructure sector stocks market',     'Infrastructure'),
    (['INTERNATIONAL', 'GLOBAL', 'US', 'NASDAQ'],        'global equity markets international index',     'International / Global'),
    (['FLEXI CAP', 'FLEXICAP', 'MULTI CAP', 'MULTICAP'], 'Indian equity market diversified stocks NSE',   'Flexi / Multi Cap'),
    (['ETF'],                                            'India ETF NSE index market',                   'ETF'),
]

def _get_fund_theme(fund_name: str):
    """Return (search_query, theme_label) for a fund based on keywords in its name."""
    name = fund_name.upper()
    for keywords, query, label in _THEME_MAP:
        if any(kw in name for kw in keywords):
            return query, label
    return 'Indian stock market mutual funds NSE BSE', 'General Equity'

# --- NewsAPI config ---
_NEWSAPI_KEY = "918c748329664746ab1b86af41376a6f"
# Trusted Indian & global financial outlets — filtered server-side by NewsAPI
_NEWSAPI_DOMAINS = (
    "economictimes.indiatimes.com,livemint.com,business-standard.com,"
    "moneycontrol.com,thehindu.com,reuters.com,businesstoday.in,"
    "financialexpress.com,ndtvprofit.com,thehindubusinessline.com"
)

@st.cache_data(ttl=3600)
def _fetch_newsapi(query: str, max_results: int = 5):
    """Fetch structured news from NewsAPI — filtered to trusted sources, cached 1 hour."""
    params = {
        "q":        query,
        "language": "en",
        "sortBy":   "publishedAt",
        "pageSize": max_results,
        "domains":  _NEWSAPI_DOMAINS,
        "apiKey":   _NEWSAPI_KEY,
    }
    resp = requests.get("https://newsapi.org/v2/everything", params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    articles = []
    for art in data.get("articles", []):
        articles.append({
            "title":  art.get("title", "Article"),
            "url":    art.get("url", ""),
            "body":   art.get("description") or "",
            "source": art.get("source", {}).get("name", ""),
            "date":   art.get("publishedAt", "")[:10],
        })
    return articles


with tab_news:
    st.subheader("Market News")
    st.caption("News grouped by each fund's investment theme, sourced from NewsAPI. Refreshes hourly.")

    # Group selected funds by theme — avoid duplicate queries for same theme
    from collections import defaultdict
    theme_groups  = defaultdict(list)   # label → [fund names]
    theme_queries = {}                  # label → search query

    for fname in mf_dfs.keys():
        query, label = _get_fund_theme(fname)
        theme_groups[label].append(fname)
        theme_queries[label] = query

    for label, fund_names in theme_groups.items():
        query = theme_queries[label]
        st.markdown(f"### {label}")
        st.caption("Funds in this theme: " + "  ·  ".join(f"`{n}`" for n in fund_names))

        try:
            articles = _fetch_newsapi(query)
            if articles:
                for art in articles:
                    with st.expander(art['title']):
                        meta = "  ·  ".join(filter(None, [art['source'], art['date']]))
                        if meta:
                            st.caption(meta)
                        if art['body']:
                            st.write(art['body'])
                        if art['url']:
                            st.markdown(f"[Read Full Article]({art['url']})")
            else:
                st.info("No recent articles found for this theme.")
        except Exception as e:
            st.warning(f"Could not load news for '{label}' ({e.__class__.__name__}). Check your connection.")

        st.write("---")

