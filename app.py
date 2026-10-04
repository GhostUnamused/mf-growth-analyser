import html
import re
import datetime
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from email.utils import parsedate_to_datetime

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
import yfinance as yf


# =============================================================================
# Constants
# =============================================================================

BENCHMARKS = {
    "Nifty 50": "^NSEI",
    "BSE Sensex": "^BSESN",
    "Nifty 500": "^CRSLDX",
    "Nifty Midcap 100": "NIFTY_MIDCAP_100.NS",
}
EXCLUDE_WORDS = ["CLOSED", "MATURED", "SUSPENDED", "IDCW", "DIVIDEND"]
PERIODS = ["1M", "6M", "YTD", "1Y", "3Y", "5Y", "10Y", "Max", "Custom"]
PERIOD_OFFSETS = {
    "1M": pd.DateOffset(months=1),
    "6M": pd.DateOffset(months=6),
    "1Y": pd.DateOffset(years=1),
    "3Y": pd.DateOffset(years=3),
    "5Y": pd.DateOffset(years=5),
    "10Y": pd.DateOffset(years=10),
}
LTCG_EXEMPTION = 125_000
# Kept upper-case when fund names are title-cased for display.
ACRONYMS = {"SBI", "ICICI", "UTI", "IDFC", "LIC", "PGIM", "ITI", "ETF", "FOF", "PSU",
            "ELSS", "IT", "US", "NASDAQ", "BSE", "NSE", "ESG", "MNC", "AMC"}
MAX_FUNDS = 5

# Categorical series colours, assigned in fixed order (never cycled). The
# benchmark is drawn in neutral grey so the funds carry the colour.
FUND_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#4a3aa7", "#e34948"]
BENCHMARK_COLOR = "#7a7974"
POS_COLOR = "#1a7f37"
NEG_COLOR = "#c62828"
GRID_COLOR = "rgba(0,0,0,0.07)"
CHART_LAYOUT = dict(
    template="plotly_white",
    font=dict(family="Source Sans Pro, sans-serif", size=12, color="#3d3c39"),
    hovermode="x unified",
    hoverlabel=dict(bgcolor="white", bordercolor="#e3e2dd", font_size=12),
    legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, title_text=""),
    margin=dict(l=8, r=8, t=10, b=8),
    plot_bgcolor="rgba(0,0,0,0)",
    paper_bgcolor="rgba(0,0,0,0)",
)

# Trusted financial outlets for NewsAPI (only used when a key is configured).
NEWSAPI_DOMAINS = (
    "economictimes.indiatimes.com,livemint.com,business-standard.com,"
    "moneycontrol.com,thehindu.com,reuters.com,businesstoday.in,"
    "financialexpress.com,ndtvprofit.com,thehindubusinessline.com"
)


# =============================================================================
# Formatting helpers
# =============================================================================

def fmt_inr(value) -> str:
    """Format a number with Indian digit grouping, e.g. ₹12,34,567."""
    try:
        v = int(round(float(value)))
    except (TypeError, ValueError):
        return "–"
    sign = "-" if v < 0 else ""
    s = str(abs(v))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while head:
            parts.insert(0, head[-2:])
            head = head[:-2]
        s = ",".join(parts) + "," + tail
    return f"₹{sign}{s}"


def fmt_inr_compact(value) -> str:
    """Compact rupee figure for headline numbers: ₹12.35 L, ₹1.20 Cr."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "–"
    if np.isnan(v):
        return "–"
    a = abs(v)
    if a >= 1e7:
        return f"₹{v / 1e7:,.2f} Cr"
    if a >= 1e5:
        return f"₹{v / 1e5:,.2f} L"
    return fmt_inr(v)


def fmt_pct(value, signed: bool = False, digits: int = 2) -> str:
    if value is None or pd.isna(value):
        return "–"
    return f"{value:+.{digits}f}%" if signed else f"{value:.{digits}f}%"


def styled_table(df: pd.DataFrame, formats: dict, signed: list[str],
                 swatches: dict) -> "pd.io.formats.style.Styler":
    """Pre-format numbers as text, colour signed columns green/red and mark
    each row with its series colour.

    Values are formatted up front because st.dataframe shows missing values
    as "None" whatever the Styler's na_rep, and mixed str/float columns fail
    Arrow serialisation.
    """
    disp = df.copy()
    for col, fn in formats.items():
        disp[col] = [("–" if pd.isna(v) else fn(v)) for v in df[col]]

    def sign_css(col):
        return ["" if pd.isna(v) or v == 0 else
                f"color: {POS_COLOR if v > 0 else NEG_COLOR}" for v in df[col.name]]

    def swatch_css(col):
        return [f"border-left: 4px solid {swatches.get(v, 'transparent')}" for v in col]

    return (disp.style.apply(sign_css, subset=signed)
            .apply(swatch_css, subset=[df.columns[0]]))


def short_name(name: str, max_len: int = 38) -> str:
    """Concise label for legends and cards; keeps Direct/Regular visible."""
    q = name.upper()
    q = re.sub(r"\(FORMERLY KNOWN AS[^)]*\)", "", q)
    q = re.sub(r"INCOME DISTRIBUTION CUM CAPITAL WITHDRAWAL OPTION.*", "", q)
    q = re.sub(r"\(PAYOUT\s*&?\s*REINVESTMENT\)", "", q)
    q = re.sub(r"\b(REINVESTMENT|PAYOUT)\b", "", q)
    q = re.sub(r"-?\s*DIRECT PLAN\b", " (D)", q)
    q = re.sub(r"-?\s*REGULAR PLAN\b", " (R)", q)
    q = re.sub(
        r"-?\s*\b(FORTNIGHTLY|MONTHLY|QUARTERLY|ANNUAL|DAILY|WEEKLY|"
        r"GROWTH OPTION|GROWTH|IDCW|BONUS|OPTION|PLAN)\b", "", q
    )
    q = re.sub(r"\s+", " ", q).strip(" -")
    q = " ".join(w if w in ACRONYMS or not re.search(r"[AEIOU]", w) else w.title()
                 for w in q.split())
    # Truncate the name, never the plan tag that tells Direct from Regular.
    m = re.search(r"\s*\((D|R)\)", q)
    plan = {"D": " (Direct)", "R": " (Regular)"}[m.group(1)] if m else ""
    q = re.sub(r"\s*\((D|R)\)", "", q).strip(" -")
    limit = max_len - len(plan)
    if len(q) > limit:
        q = q[:limit].rstrip(" -") + "…"
    return q + plan


# =============================================================================
# Data fetching
# Cached functions raise on failure: st.cache_data does not cache exceptions,
# so a transient network error is retried on the next run instead of being
# remembered for the whole TTL.
# =============================================================================

@st.cache_data(ttl=86400, show_spinner=False)
def fetch_active_funds() -> dict:
    res = requests.get("https://www.amfiindia.com/spages/NAVAll.txt", timeout=20)
    res.raise_for_status()
    funds = {}
    for line in res.text.splitlines():
        parts = line.split(";")
        if len(parts) >= 6 and parts[0].strip().isdigit():
            name = parts[3].strip().upper()
            if name and not any(w in name for w in EXCLUDE_WORDS):
                funds[name] = parts[0].strip()
    if not funds:
        raise ValueError("AMFI fund list was empty")
    return dict(sorted(funds.items()))


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_nav_history(scheme_code: str) -> pd.DataFrame:
    res = requests.get(f"https://api.mfapi.in/mf/{scheme_code}", timeout=20)
    res.raise_for_status()
    data = res.json().get("data") or []
    if not data:
        raise ValueError(f"No NAV history for scheme {scheme_code}")
    df = pd.DataFrame(data)
    df["Date"] = pd.to_datetime(df["date"], format="%d-%m-%Y")
    df["NAV"] = pd.to_numeric(df["nav"], errors="coerce")
    df = df[df["NAV"] > 0].drop_duplicates("Date").sort_values("Date")
    return df[["Date", "NAV"]].reset_index(drop=True)


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_index_history(ticker: str, start: str = "2000-01-01") -> pd.DataFrame:
    df = yf.download(ticker, start=start, progress=False, auto_adjust=True)
    if df is None or df.empty:
        raise ValueError(f"No data for {ticker}")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.reset_index()[["Date", "Close"]].dropna()
    df["Date"] = pd.to_datetime(df["Date"]).dt.tz_localize(None).dt.normalize()
    return df.rename(columns={"Close": "Index"}).sort_values("Date").reset_index(drop=True)


# =============================================================================
# Finance maths
# =============================================================================

def xirr(flows) -> float:
    """Annualised money-weighted return (%) for [(date, amount), ...].

    Outflows (investments) are negative, the closing value positive.
    Solved by bisection, which is robust for the single sign-change cash
    flows produced by lumpsum and SIP investing.
    """
    if len(flows) < 2:
        return np.nan
    d0 = flows[0][0]
    t = np.array([(d - d0).days / 365.25 for d, _ in flows])
    cf = np.array([a for _, a in flows], dtype=float)
    if t[-1] <= 0 or not (cf < 0).any() or not (cf > 0).any():
        return np.nan

    def npv(r):
        return float(np.sum(cf / (1.0 + r) ** t))

    lo, hi = -0.9999, 10.0
    f_lo, f_hi = npv(lo), npv(hi)
    if np.sign(f_lo) == np.sign(f_hi):
        return np.nan
    for _ in range(200):
        mid = (lo + hi) / 2
        f_mid = npv(mid)
        if abs(f_mid) < 1e-6:
            break
        if np.sign(f_mid) == np.sign(f_lo):
            lo, f_lo = mid, f_mid
        else:
            hi = mid
    return mid * 100


def price_cagr(series: pd.Series) -> float:
    """CAGR (%) of a date-indexed price series."""
    s = series.dropna()
    if len(s) < 2:
        return np.nan
    yrs = (s.index[-1] - s.index[0]).days / 365.25
    if yrs <= 0 or s.iloc[0] <= 0:
        return np.nan
    return ((s.iloc[-1] / s.iloc[0]) ** (1 / yrs) - 1) * 100


def simulate(dates: pd.Series, prices: pd.Series, mode: str, amount: float,
             step_up: float, start_idx: int = 0) -> dict | None:
    """Simulate a lumpsum or SIP on a price series aligned to `dates`.

    `prices` may contain NaN before the asset's inception; nothing is
    invested before the first valid price, so a fund launched mid-period is
    never "bought" at a back-filled NAV.
    """
    p = prices.ffill().to_numpy(dtype=float)
    n = len(p)
    valid = ~np.isnan(p)
    valid[:start_idx] = False
    if not valid.any():
        return None
    first = int(np.argmax(valid))

    contrib = np.zeros(n)
    units = np.zeros(n)
    if mode == "Lumpsum":
        contrib[first] = amount
        units[first] = amount / p[first]
    else:
        months = dates.dt.to_period("M").to_numpy()
        seen, k = set(), 0
        for i in range(first, n):
            if months[i] in seen:
                continue
            seen.add(months[i])
            instalment = amount * (1 + step_up / 100) ** (k // 12)
            contrib[i] = instalment
            units[i] = instalment / p[i]
            k += 1

    value = np.cumsum(units) * p
    invested = np.cumsum(contrib)
    value[:first] = np.nan
    invested[:first] = np.nan
    return {"value": value, "invested": invested, "contrib": contrib, "first": first}


def apply_tax_inflation(sim: dict, dates: pd.Series, tax_rate: float,
                        inflation: float) -> dict:
    """Express a simulation in post-tax, start-of-period rupees.

    Tax: LTCG on gains above the ₹1.25 L exemption, as if redeemed on that day.
    Inflation: every amount (value and each contribution) is deflated back to
    the first date of the selected period, so returns become real returns.
    """
    yrs = (dates - dates.iloc[0]).dt.days.to_numpy() / 365.25
    deflator = (1 + inflation / 100) ** yrs
    gain = sim["value"] - sim["invested"]
    tax = np.clip(gain - LTCG_EXEMPTION, 0, None) * tax_rate / 100
    real_contrib = sim["contrib"] / deflator
    invested = np.cumsum(real_contrib)
    invested[: sim["first"]] = np.nan
    return {
        "value": (sim["value"] - tax) / deflator,
        "invested": invested,
        "contrib": real_contrib,
        "first": sim["first"],
    }


def summarise(sim: dict, dates: pd.Series) -> dict:
    """Headline numbers for a simulation."""
    value, invested = sim["value"][-1], sim["invested"][-1]
    flows = [(dates.iloc[i], -c) for i, c in enumerate(sim["contrib"]) if c > 0]
    flows.append((dates.iloc[-1], value))
    years = (dates.iloc[-1] - dates.iloc[sim["first"]]).days / 365.25
    return {
        "invested": invested,
        "value": value,
        "gain": value - invested,
        "abs_ret": (value / invested - 1) * 100 if invested > 0 else np.nan,
        # Annualising sub-one-year returns is misleading (AMFI convention).
        "ann_ret": xirr(flows) if years >= 1 else np.nan,
        "years": years,
        "start": dates.iloc[sim["first"]],
    }


def risk_metrics(series: pd.Series) -> dict:
    """Risk statistics from an asset's own (un-filled) price observations."""
    s = series.dropna()
    s = s[~s.index.duplicated()]
    out = dict.fromkeys(["best_1y", "worst_1y", "max_dd", "vol", "loss_years",
                         "cagr", "double"], np.nan)
    if len(s) < 2:
        return out

    # Rolling one-year returns, by calendar date rather than row count.
    lag_dates = s.index - pd.DateOffset(years=1)
    eligible = lag_dates >= s.index[0]
    if eligible.any():
        lagged = s.reindex(lag_dates[eligible], method="ffill").to_numpy()
        roll = (s[eligible].to_numpy() / lagged - 1) * 100
        out["best_1y"], out["worst_1y"] = np.nanmax(roll), np.nanmin(roll)

    out["max_dd"] = ((s / s.cummax()) - 1).min() * 100
    rets = s.pct_change().dropna()
    if len(rets) > 1:
        out["vol"] = rets.std() * np.sqrt(252) * 100

    year_end = s.resample("YE").last()
    out["loss_years"] = int((year_end.pct_change().dropna() < 0).sum())

    out["cagr"] = price_cagr(s)
    if out["cagr"] > 0:
        out["double"] = np.log(2) / np.log(1 + out["cagr"] / 100)
    return out


# =============================================================================
# News
# =============================================================================

# (keywords matched as whole words, search query, label). More specific first.
THEMES = [
    (["NIFTY 50", "NIFTY50", "NIFTY NEXT 50", "LARGE CAP", "LARGE-CAP", "LARGECAP", "BLUECHIP", "BLUE CHIP"],
     "Nifty 50 large cap stocks India", "Large Cap"),
    (["MIDCAP", "MID CAP", "MID-CAP"], "midcap stocks India NSE", "Mid Cap"),
    (["SMALLCAP", "SMALL CAP", "SMALL-CAP"], "smallcap stocks India NSE", "Small Cap"),
    (["SENSEX"], "Sensex BSE India stocks", "Sensex"),
    (["BANKING & PSU", "BANKING AND PSU", "PSU DEBT"], "banking PSU bonds India RBI", "Banking & PSU Debt"),
    (["BANKING", "BANK", "FINANCIAL SERVICES"], "Indian banking sector stocks", "Banking & Financial Services"),
    (["CORPORATE BOND", "CREDIT RISK"], "India corporate bond yields", "Corporate Bonds"),
    (["OVERNIGHT", "LIQUID", "MONEY MARKET"], "RBI repo rate money market India", "Money Market"),
    (["GILT", "GSEC", "G-SEC", "GOVERNMENT SECURITIES"], "India government bond yields RBI", "Gilt"),
    (["IT", "TECHNOLOGY", "TECH", "DIGITAL"], "India IT sector stocks Infosys TCS", "Technology"),
    (["PHARMA", "HEALTHCARE", "HEALTH CARE"], "India pharma healthcare stocks", "Pharma & Healthcare"),
    (["INFRASTRUCTURE", "INFRA"], "India infrastructure stocks", "Infrastructure"),
    (["GOLD", "SILVER"], "gold silver prices India", "Precious Metals"),
    (["INTERNATIONAL", "GLOBAL", "US", "NASDAQ", "S&P 500", "FOF OVERSEAS"], "global equity markets US stocks", "International"),
    (["ELSS", "TAX SAVER"], "ELSS tax saver mutual funds India", "ELSS / Tax Saver"),
    (["HYBRID", "BALANCED", "ARBITRAGE", "MULTI ASSET", "EQUITY & DEBT", "EQUITY SAVINGS"], "hybrid mutual funds India market", "Hybrid"),
    (["FLEXI CAP", "FLEXICAP", "MULTI CAP", "MULTICAP", "FOCUSED", "VALUE", "CONTRA"],
     "Indian equity market Nifty Sensex", "Diversified Equity"),
]


def fund_theme(fund_name: str) -> tuple[str, str]:
    name = fund_name.upper()
    for keywords, query, label in THEMES:
        # Whole-word match: plain substring matching sent every "EQUITY"
        # fund to Technology ("IT") and "FOCUSED" funds to International ("US").
        if any(re.search(rf"(?<![A-Z0-9]){re.escape(kw)}(?![A-Z0-9])", name) for kw in keywords):
            return query, label
    return "Indian stock market mutual funds", "Indian Markets"


def _newsapi_key() -> str | None:
    try:
        return st.secrets.get("NEWSAPI_KEY")
    except Exception:
        return None


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_news(query: str, limit: int = 6) -> list[dict]:
    key = _newsapi_key()
    if key:
        res = requests.get("https://newsapi.org/v2/everything", timeout=10, params={
            "q": query, "language": "en", "sortBy": "publishedAt",
            "pageSize": limit, "domains": NEWSAPI_DOMAINS, "apiKey": key,
        })
        res.raise_for_status()
        return [{
            "title": a.get("title") or "Untitled",
            "url": a.get("url") or "",
            "source": (a.get("source") or {}).get("name", ""),
            "date": (a.get("publishedAt") or "")[:10],
        } for a in res.json().get("articles", [])]

    # Keyless fallback: Google News RSS.
    res = requests.get("https://news.google.com/rss/search", timeout=10, params={
        "q": f"{query} when:14d", "hl": "en-IN", "gl": "IN", "ceid": "IN:en",
    })
    res.raise_for_status()
    items = []
    for item in ET.fromstring(res.content).iter("item"):
        title = item.findtext("title") or "Untitled"
        source = item.findtext("source") or ""
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3]
        try:
            date = parsedate_to_datetime(item.findtext("pubDate")).strftime("%Y-%m-%d")
        except Exception:
            date = ""
        items.append({"title": title, "url": item.findtext("link") or "",
                      "source": source, "date": date})
        if len(items) >= limit:
            break
    return items


# =============================================================================
# Page setup
# =============================================================================

st.set_page_config(page_title="MF Growth Analyser", layout="wide")

st.markdown(
    """
    <style>
      .block-container { padding-top: 2.2rem; padding-bottom: 3rem; max-width: 1320px; }
      h1 { font-weight: 650; letter-spacing: -0.02em; margin-bottom: 0 !important; }
      h3 { font-weight: 600; letter-spacing: -0.01em; }
      [data-testid="stMetricLabel"] p { font-size: 0.82rem; color: #5f5e5a; }
      [data-testid="stMetricValue"] { font-size: 1.55rem; font-variant-numeric: tabular-nums; }
      .mfga-sub { color: #5f5e5a; font-size: 0.95rem; margin: 0.15rem 0 1.2rem; }
      .mfga-swatch { display: inline-block; width: 10px; height: 10px; border-radius: 2px;
                     margin-right: 6px; vertical-align: baseline; }
      .mfga-news { padding: 0.55rem 0; border-bottom: 1px solid rgba(0,0,0,0.07); }
      .mfga-news a { color: inherit; text-decoration: none; font-weight: 500; }
      .mfga-news a:hover { text-decoration: underline; }
      .mfga-meta { color: #75746f; font-size: 0.8rem; margin-top: 0.1rem; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("MF Growth Analyser")
st.markdown(
    '<p class="mfga-sub">Back-test Indian mutual funds against a market index '
    "with lumpsum or SIP investing, using daily NAVs from AMFI.</p>",
    unsafe_allow_html=True,
)

# --- Fund selection ----------------------------------------------------------
try:
    with st.spinner("Loading fund list…"):
        all_funds = fetch_active_funds()
except Exception as exc:
    st.error(f"Could not load the AMFI fund list ({exc.__class__.__name__}). "
             "Please refresh in a minute.")
    st.stop()

with st.container(border=True):
    c_search, c_pick = st.columns([1, 3])
    fund_search = c_search.text_input(
        "Search funds", placeholder="AMC, category or keyword", key="fund_search_input"
    )
    terms = fund_search.upper().split()
    filtered = [n for n in all_funds if all(t in n for t in terms)] if terms else list(all_funds)
    # Keep current picks in the options so a new search never drops them.
    current = st.session_state.get("fund_multiselect", [])
    options = list(dict.fromkeys(current + filtered))
    selected_funds = c_pick.multiselect(
        f"Funds to compare (up to {MAX_FUNDS})",
        options=options,
        max_selections=MAX_FUNDS,
        format_func=lambda n: short_name(n, 48),
        placeholder=f"{len(filtered):,} funds match — choose up to {MAX_FUNDS}",
        key="fund_multiselect",
    )

    c1, c2, c3, c4, c5 = st.columns([1.1, 1, 1.1, 1, 1.2])
    benchmark_name = c1.selectbox("Benchmark", list(BENCHMARKS), index=0)
    mode = c2.segmented_control("Investment", ["SIP", "Lumpsum"], default="SIP",
                                key="mode") or "SIP"
    amount = c3.number_input(
        "Monthly SIP (₹)" if mode == "SIP" else "Lumpsum (₹)",
        min_value=500, value=10_000 if mode == "SIP" else 1_00_000, step=500,
    )
    c3.caption(fmt_inr(amount))
    step_up = c4.number_input("Annual step-up (%)", 0, 50, 0, 1, key="step_up",
                              disabled=(mode != "SIP"))
    step_up = step_up if mode == "SIP" else 0
    real_terms = c5.toggle("Post-tax, inflation-adjusted",
                           help="Deducts LTCG tax (above the ₹1.25 L exemption) as if "
                                "redeemed on each date, and expresses all values in "
                                "rupees of the period's start date.")
    if real_terms:
        t1, t2 = c5.columns(2)
        tax_rate = t1.number_input("LTCG %", 0.0, 50.0, 12.5, 0.5)
        inflation = t2.number_input("Inflation %", 0.0, 20.0, 5.5, 0.5)
    else:
        tax_rate = inflation = 0.0

if not selected_funds:
    st.info("Search for and select at least one fund to begin.")
    st.stop()

# --- Fetch data --------------------------------------------------------------
with st.spinner("Fetching NAV and index history…"):
    try:
        index_df = fetch_index_history(BENCHMARKS[benchmark_name])
    except Exception as exc:
        st.error(f"Could not load {benchmark_name} data from Yahoo Finance "
                 f"({exc.__class__.__name__}). Try another benchmark or refresh.")
        st.stop()

    nav = {}
    failed = []
    for fname in selected_funds:
        try:
            nav[fname] = fetch_nav_history(all_funds[fname]).rename(columns={"NAV": fname})
        except Exception:
            failed.append(fname)

if failed:
    st.warning("No NAV history available for: " + ", ".join(short_name(f) for f in failed))
if not nav:
    st.stop()

funds = list(nav)
merged = index_df
for df in nav.values():
    merged = merged.merge(df, on="Date", how="outer")
merged = merged.sort_values("Date").reset_index(drop=True)

# Labels and colours follow the fund (selection order), never its rank.
_shorts = {f: short_name(f) for f in funds}
_dupes = Counter(_shorts.values())
_seen = Counter()
LABEL = {"Index": benchmark_name}
for f in funds:
    s = _shorts[f]
    if _dupes[s] > 1:
        _seen[s] += 1
        s = f"{s} #{_seen[s]}"
    LABEL[f] = s
COLOR = {"Index": BENCHMARK_COLOR}
for i, f in enumerate(selected_funds):
    if f in nav:
        COLOR[f] = FUND_COLORS[i % len(FUND_COLORS)]
assets = ["Index"] + funds

# --- Period selection --------------------------------------------------------
first_obs = merged.dropna(subset=assets, how="all")["Date"]
min_date, max_date = first_obs.min(), merged["Date"].max()

pc1, pc2 = st.columns([3, 2], vertical_alignment="bottom")
period = pc1.segmented_control("Period", PERIODS, default="3Y", key="period") or "3Y"
if period == "Custom":
    rng = pc2.slider("Date range", min_value=min_date.date(), max_value=max_date.date(),
                     value=(max(min_date, max_date - pd.DateOffset(years=3)).date(),
                            max_date.date()),
                     format="MMM YYYY")
    start_dt, end_dt = pd.Timestamp(rng[0]), pd.Timestamp(rng[1])
else:
    end_dt = max_date
    if period == "YTD":
        start_dt = pd.Timestamp(max_date.year, 1, 1)
    elif period == "Max":
        start_dt = min_date
    else:
        start_dt = max_date - PERIOD_OFFSETS[period]
    start_dt = max(start_dt, min_date)

window = merged[(merged["Date"] >= start_dt) & (merged["Date"] <= end_dt)].reset_index(drop=True)
if len(window) < 2:
    st.warning("Not enough data in the selected range.")
    st.stop()
dates = window["Date"]
start_dt, end_dt = dates.iloc[0], dates.iloc[-1]
period_label = "custom range" if period == "Custom" else period

# --- Simulate ----------------------------------------------------------------
def run(asset: str, start_idx: int = 0):
    sim = simulate(dates, window[asset], mode, amount, step_up, start_idx)
    if sim is not None and real_terms:
        sim = apply_tax_inflation(sim, dates, tax_rate, inflation)
    return sim


sims = {a: run(a) for a in assets}
missing = [a for a in assets if sims[a] is None]
for a in missing:
    st.warning(f"{LABEL[a]} has no data in the selected period.")
assets = [a for a in assets if sims[a] is not None]
funds = [f for f in funds if f in assets]
if not funds:
    st.stop()

summary = {a: summarise(sims[a], dates) for a in assets}

# Outperformance is measured against the benchmark over the fund's own
# window (same start date, same instalments), so a fund launched mid-period
# is not compared with a longer benchmark history.
for f in funds:
    bm = summary.get("Index")
    if bm is not None and sims[f]["first"] != sims["Index"]["first"]:
        bm_sim = run("Index", sims[f]["first"])
        bm = summarise(bm_sim, dates) if bm_sim is not None else None
    summary[f]["vs_bm"] = (summary[f]["ann_ret"] - bm["ann_ret"]
                           if bm is not None else np.nan)

late_starters = [f for f in funds if summary[f]["start"] > start_dt + pd.Timedelta(days=7)]

# =============================================================================
# Output
# =============================================================================

value_word = "Real value" if real_terms else "Value"
st.markdown(
    f"**{mode}** of **{fmt_inr(amount)}**"
    f"{' per month' if mode == 'SIP' else ''}"
    f"{f', stepped up {step_up}% a year' if mode == 'SIP' and step_up else ''}"
    f" · {start_dt:%d %b %Y} – {end_dt:%d %b %Y}"
    f"{' · post-tax, in start-date rupees' if real_terms else ''}"
)

tab_perf, tab_risk, tab_plan, tab_news = st.tabs(
    ["Performance", "Risk", "Goal planner", "News"]
)

# --- Performance -------------------------------------------------------------
with tab_perf:
    cols = st.columns(len(assets))
    for col, a in zip(cols, assets):
        s = summary[a]
        ret = s["ann_ret"] if not pd.isna(s["ann_ret"]) else s["abs_ret"]
        ret_kind = "a.a." if not pd.isna(s["ann_ret"]) else "abs."
        col.metric(
            label=LABEL[a] + (" · benchmark" if a == "Index" else ""),
            value=fmt_inr_compact(s["value"]),
            delta=f"{fmt_pct(ret, signed=True)} {ret_kind}",
            border=True,
            help=f"{a if a != 'Index' else benchmark_name}\n\n"
                 f"Invested {fmt_inr(s['invested'])} · gain {fmt_inr(s['gain'])}",
        )

    fig = go.Figure()
    for a in assets:
        is_bm = a == "Index"
        fig.add_trace(go.Scatter(
            x=dates, y=sims[a]["value"], name=LABEL[a], mode="lines",
            line=dict(color=COLOR[a], width=2, dash="dot" if is_bm else "solid"),
            hovertemplate=f"{LABEL[a]}: %{{y:,.0f}}<extra></extra>",
        ))
    invested_ref = sims[funds[0]]
    if all(sims[f]["first"] == invested_ref["first"] for f in funds):
        fig.add_trace(go.Scatter(
            x=dates, y=invested_ref["invested"], name="Amount invested", mode="lines",
            line=dict(color="#b5b4ae", width=1.5, shape="hv"),
            hovertemplate="Invested: %{y:,.0f}<extra></extra>",
        ))
    fig.update_layout(
        **(CHART_LAYOUT | {"margin": dict(l=8, r=8, t=48, b=8)}),
        height=460,
        yaxis=dict(title=f"{value_word} (₹)", tickprefix="₹", tickformat=",.0f",
                   gridcolor=GRID_COLOR, zeroline=False, automargin=True, rangemode="tozero"),
        xaxis=dict(showgrid=False, automargin=True),
    )
    st.plotly_chart(fig, width="stretch", theme=None,
                    config={"displaylogo": False, "modeBarButtonsToRemove": ["select2d", "lasso2d"]})
    if late_starters:
        st.caption("Launched after the period start, so simulated from first NAV: " +
                   "; ".join(f"{LABEL[f]} ({summary[f]['start']:%b %Y})" for f in late_starters))

    perf_rows = []
    for a in assets:
        s = summary[a]
        perf_rows.append({
            "Fund": LABEL[a] + (" (benchmark)" if a == "Index" else ""),
            "Invested": s["invested"],
            value_word: s["value"],
            "Gain": s["gain"],
            "Absolute return": s["abs_ret"],
            "XIRR" if mode == "SIP" else "CAGR": s["ann_ret"],
            f"vs {benchmark_name}": s.get("vs_bm", np.nan),
        })
    perf_df = pd.DataFrame(perf_rows)
    ret_col = "XIRR" if mode == "SIP" else "CAGR"
    color_by_label = {LABEL[a] + (" (benchmark)" if a == "Index" else ""): COLOR[a] for a in assets}

    st.dataframe(
        styled_table(
            perf_df,
            {"Invested": fmt_inr, value_word: fmt_inr, "Gain": fmt_inr,
             "Absolute return": fmt_pct, ret_col: fmt_pct,
             f"vs {benchmark_name}": lambda v: fmt_pct(v, signed=True)},
            signed=["Gain", "Absolute return", ret_col, f"vs {benchmark_name}"],
            swatches=color_by_label,
        ),
        width="stretch", hide_index=True,
    )
    st.caption(
        f"{ret_col} is shown only for periods of a year or more. "
        f"“vs {benchmark_name}” compares annualised returns over each fund's own "
        "investment window. The benchmark is a price index (dividends excluded), "
        "so it slightly understates the index's total return."
    )

    export = perf_df.copy()
    export.insert(1, "Scheme", ["" if a == "Index" else a for a in assets])
    st.download_button("Download results (CSV)", export.to_csv(index=False),
                       file_name="mf_analysis.csv", mime="text/csv")

# --- Risk --------------------------------------------------------------------
with tab_risk:
    series = {a: window.set_index("Date")[a].dropna() for a in assets}
    risk_rows = []
    for a in assets:
        r = risk_metrics(series[a])
        risk_rows.append({
            "Fund": LABEL[a] + (" (benchmark)" if a == "Index" else ""),
            "Price CAGR": r["cagr"],
            "Volatility (ann.)": r["vol"],
            "Max drawdown": r["max_dd"],
            "Best 1Y": r["best_1y"],
            "Worst 1Y": r["worst_1y"],
            "Down years": r["loss_years"],
            "Years to double": r["double"],
        })
    risk_df = pd.DataFrame(risk_rows)
    st.dataframe(
        styled_table(
            risk_df,
            {c: fmt_pct for c in ["Price CAGR", "Volatility (ann.)", "Max drawdown",
                                  "Best 1Y", "Worst 1Y"]}
            | {"Years to double": lambda v: f"{v:.1f}", "Down years": lambda v: f"{int(v)}"},
            signed=["Price CAGR", "Best 1Y", "Worst 1Y"],
            swatches=color_by_label,
        ),
        width="stretch", hide_index=True,
    )

    st.markdown("##### Drawdown from previous peak")
    dd = go.Figure()
    dd_floor = 0.0
    for a in assets:
        s = series[a]
        drawdown = (s / s.cummax() - 1) * 100
        dd_floor = min(dd_floor, drawdown.min())
        dd.add_trace(go.Scatter(
            x=s.index, y=drawdown, name=LABEL[a], mode="lines",
            line=dict(color=COLOR[a], width=1.5, dash="dot" if a == "Index" else "solid"),
            hovertemplate=f"{LABEL[a]}: %{{y:.1f}}%<extra></extra>",
        ))
    dd.update_layout(
        **(CHART_LAYOUT | {"margin": dict(l=8, r=8, t=56, b=8)}),
        height=360,
        yaxis=dict(ticksuffix="%", gridcolor=GRID_COLOR, zeroline=True,
                   range=[dd_floor * 1.08 - 1, 2],
                   zerolinecolor="rgba(0,0,0,0.25)", automargin=True),
        xaxis=dict(showgrid=False, automargin=True),
    )
    st.plotly_chart(dd, width="stretch", theme=None, config={"displaylogo": False})

    with st.expander("How these are calculated"):
        st.markdown(
            "All risk figures use each fund's own daily NAVs within the selected period, "
            "independent of the investment mode.\n\n"
            "- **Price CAGR** — annualised growth of the NAV (or index level).\n"
            "- **Volatility** — standard deviation of daily returns × √252.\n"
            "- **Max drawdown** — the largest fall from a previous peak.\n"
            "- **Best / worst 1Y** — range of returns over every rolling 12-month window.\n"
            "- **Down years** — calendar years that ended lower than the previous year-end.\n"
            "- **Years to double** — ln 2 ÷ ln(1 + CAGR), at the period's CAGR."
        )

# --- Goal planner ------------------------------------------------------------
with tab_plan:
    g1, g2, _ = st.columns([1, 1, 2])
    goal = g1.number_input("Target corpus (₹)", min_value=10_000, value=1_00_00_000,
                           step=1_00_000)
    g1.caption(fmt_inr(goal))
    horizon = g2.number_input("Years to goal", min_value=1, max_value=50, value=10)
    goal_nominal = goal * (1 + inflation / 100) ** horizon if real_terms else goal
    n = int(horizon * 12)

    plan_rows = []
    for a in assets:
        cagr = price_cagr(series[a])
        if pd.isna(cagr) or cagr <= 0:
            sip = np.nan  # no meaningful projection from a flat or negative return
        else:
            r = (1 + cagr / 100) ** (1 / 12) - 1
            # Future value of an annuity due: FV = P × ((1+r)^n − 1) / r × (1+r)
            sip = goal_nominal * r / (((1 + r) ** n - 1) * (1 + r))
        plan_rows.append({
            "Fund": LABEL[a] + (" (benchmark)" if a == "Index" else ""),
            f"CAGR ({period_label})": cagr,
            "Monthly SIP needed": sip,
            "Total you invest": sip * n,
        })
    st.dataframe(
        styled_table(
            pd.DataFrame(plan_rows),
            {f"CAGR ({period_label})": fmt_pct, "Monthly SIP needed": fmt_inr,
             "Total you invest": fmt_inr},
            signed=[f"CAGR ({period_label})"],
            swatches=color_by_label,
        ),
        width="stretch", hide_index=True,
    )
    note = (f"Assumes each fund repeats its {period_label} CAGR and a flat SIP at the start "
            "of each month. Funds with a zero or negative CAGR are left blank.")
    if real_terms:
        note += (f" The target is in today's rupees, grossed up at {inflation}% inflation to "
                 f"{fmt_inr(goal_nominal)}; tax is not deducted.")
    if (end_dt - start_dt).days < 365:
        note += " Periods under a year give unreliable CAGRs — pick 3Y or longer."
    st.caption(note + " Past returns do not guarantee future returns.")

# --- News --------------------------------------------------------------------
with tab_news:
    groups = defaultdict(list)
    queries = {}
    for f in funds:
        q, label = fund_theme(f)
        groups[label].append(f)
        queries[label] = q

    news_cols = st.columns(min(len(groups), 2))
    for i, (label, members) in enumerate(groups.items()):
        with news_cols[i % len(news_cols)]:
            st.markdown(f"##### {label}")
            st.markdown(
                " ".join(f'<span class="mfga-swatch" style="background:{COLOR[f]}"></span>'
                         f'<span class="mfga-meta">{LABEL[f]}</span>&nbsp;&nbsp;' for f in members),
                unsafe_allow_html=True,
            )
            try:
                articles = fetch_news(queries[label])
            except Exception as exc:
                st.caption(f"News unavailable ({exc.__class__.__name__}).")
                continue
            if not articles:
                st.caption("No recent articles.")
            for art in articles:
                title = html.escape(art["title"])
                url = html.escape(art["url"], quote=True)
                meta = " · ".join(x for x in [art["source"], art["date"]] if x)
                st.markdown(
                    f'<div class="mfga-news"><a href="{url}" target="_blank" '
                    f'rel="noopener">{title}</a><div class="mfga-meta">{meta}</div></div>',
                    unsafe_allow_html=True,
                )
    st.caption("Headlines are matched to each fund's theme by keywords in its name and "
               "refresh hourly.")

st.divider()
st.caption(
    f"Data: AMFI and mfapi.in (NAV), Yahoo Finance (index). Latest data point "
    f"{max_date:%d %b %Y}. For education only — not investment advice."
)
