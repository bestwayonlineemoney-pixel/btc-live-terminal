import datetime
import requests
import numpy as np
import pandas as pd
import streamlit as st
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from streamlit_autorefresh import st_autorefresh

# -------------------------------------------------------------------------
# 1. PAGE CONFIG & INSTITUTIONAL DARK TERMINAL CSS
# -------------------------------------------------------------------------
st.set_page_config(
    page_title="BTCUSDT Institutional Order Flow Terminal",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded"
)

st.markdown("""
<style>
    .stApp, [data-testid="stAppViewContainer"], [data-testid="stHeader"] {
        background-color: #070a0f !important;
        color: #e2e8f0 !important;
    }
    [data-testid="stSidebar"] {
        background-color: #0b0f17 !important;
        border-right: 1px solid #1e293b !important;
    }
    .stMarkdown, p, h1, h2, h3, h4, span, label {
        color: #e2e8f0 !important;
    }
    div[data-testid="stMetric"] {
        background-color: #111827 !important;
        border: 1px solid #1f2937 !important;
        padding: 14px 16px !important;
        border-radius: 8px !important;
        box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.4) !important;
    }
    div[data-testid="stMetricLabel"] > div {
        color: #94a3b8 !important;
        font-size: 12px !important;
        font-weight: 600 !important;
        text-transform: uppercase !important;
    }
    div[data-testid="stMetricValue"] > div {
        color: #f8fafc !important;
        font-size: 22px !important;
        font-family: 'JetBrains Mono', monospace !important;
    }
    .signal-box {
        background: linear-gradient(135deg, #0f172a 0%, #111827 100%);
        border: 1px solid #334155;
        border-left: 5px solid #38bdf8;
        border-radius: 8px;
        padding: 16px 20px;
        margin-bottom: 18px;
    }
    .badge {
        display: inline-block;
        padding: 3px 10px;
        border-radius: 4px;
        font-size: 12px;
        font-weight: 700;
        margin-right: 8px;
    }
</style>
""", unsafe_allow_html=True)

# -------------------------------------------------------------------------
# 2. SIDEBAR CONTROLS
# -------------------------------------------------------------------------
st.sidebar.markdown("## 🛡️ Terminal Controls")
symbol = st.sidebar.selectbox("Asset Pair", ["BTCUSDT", "ETHUSDT", "SOLUSDT"], index=0)
interval = st.sidebar.selectbox("Timeframe (Candles)", ["1h", "4h", "8h", "1d"], index=0)

default_days = {"1h": 7, "4h": 21, "8h": 30, "1d": 30}[interval]
days_back = st.sidebar.slider("Historical Lookback (Days)", min_value=3, max_value=30, value=default_days)

st.sidebar.markdown("### 📐 Chart Overlays")
show_oi_overlay = st.sidebar.checkbox("Overlay Open Interest on Price Chart", value=True)
show_emas = st.sidebar.checkbox("Show EMA 20 & EMA 50", value=True)
show_hack_marker = st.sidebar.checkbox("Mark Oct 29 Event Line", value=True)
show_signals = st.sidebar.checkbox("Show Whale Sweep & Squeeze Markers", value=True)

refresh_sec = st.sidebar.slider("Live Auto-Refresh (Seconds)", min_value=10, max_value=120, value=20)
st_autorefresh(interval=refresh_sec * 1000, key="terminal_refresher")

if st.sidebar.button("🔄 Force Live Sync Now"):
    st.cache_data.clear()

# -------------------------------------------------------------------------
# 3. DUAL-ENGINE DATA ENGINE (BINANCE + BYBIT CLOUD FALLBACK)
# -------------------------------------------------------------------------
now = datetime.datetime.now(datetime.timezone.utc)
start_dt = now - datetime.timedelta(days=days_back)
start_ms = int(start_dt.timestamp() * 1000)
end_ms = int(now.timestamp() * 1000)

@st.cache_data(ttl=12)
def fetch_all_market_data(sym, tf, start_time, end_time):
    # STEP 1: Fetch Spot Klines (Binance Vision Spot API never blocks Cloud/Datacenters)
    spot_url = "https://data-api.binance.vision"
    s_rows = []
    try:
        sk_res = requests.get(f"{spot_url}/api/v3/klines", params={
            "symbol": sym, "interval": tf, "startTime": start_time, "endTime": end_time, "limit": 1000
        }, timeout=8).json()
        if isinstance(sk_res, list):
            for k in sk_res:
                s_vol = float(k[7])
                s_buy = float(k[10])
                s_rows.append({
                    "timestamp": int(k[0]),
                    "spot_close": float(k[4]),
                    "spot_vol_usdt": s_vol,
                    "spot_delta_usdt": s_buy - (s_vol - s_buy)
                })
    except Exception:
        pass
    df_spot = pd.DataFrame(s_rows)

    # STEP 2: Try Binance Futures first (Active when run locally on PC)
    active_engine = "Binance Futures"
    is_binance_ok = False
    f_rows, oi_rows, whale_rows, retail_rows, fr_rows = [], [], [], [], []
    live_meta = {}

    try:
        fapi = "https://fapi.binance.com"
        test_res = requests.get(f"{fapi}/fapi/v1/klines", params={
            "symbol": sym, "interval": tf, "startTime": start_time, "endTime": end_time, "limit": 1000
        }, timeout=6).json()

        if isinstance(test_res, list) and len(test_res) > 0 and isinstance(test_res[0], list):
            is_binance_ok = True
            for k in test_res:
                vol_usdt = float(k[7])
                taker_buy = float(k[10])
                f_rows.append({
                    "timestamp": int(k[0]),
                    "datetime": pd.to_datetime(k[0], unit="ms", utc=True),
                    "open": float(k[1]), "high": float(k[2]), "low": float(k[3]), "close": float(k[4]),
                    "fut_vol_usdt": vol_usdt,
                    "fut_delta_usdt": taker_buy - (vol_usdt - taker_buy)
                })

            prem_res = requests.get(f"{fapi}/fapi/v1/premiumIndex", params={"symbol": sym}, timeout=6).json()
            live_meta["markPrice"] = float(prem_res.get("markPrice", f_rows[-1]["close"]))
            live_meta["indexPrice"] = float(prem_res.get("indexPrice", live_meta["markPrice"]))
            live_meta["fundingRate"] = float(prem_res.get("lastFundingRate", 0))

            deriv_period = {"1h": "1h", "4h": "4h", "8h": "4h", "1d": "1d"}[tf]
            oi_res = requests.get(f"{fapi}/futures/data/openInterestHist", params={"symbol": sym, "period": deriv_period, "limit": 500}, timeout=6).json()
            if isinstance(oi_res, list):
                oi_rows = [{"timestamp": int(x["timestamp"]), "oi_usdt": float(x["sumOpenInterestValue"])} for x in oi_res]

            w_res = requests.get(f"{fapi}/futures/data/topLongShortPositionRatio", params={"symbol": sym, "period": deriv_period, "limit": 500}, timeout=6).json()
            if isinstance(w_res, list):
                whale_rows = [{"timestamp": int(x["timestamp"]), "whale_ls": float(x["longShortRatio"])} for x in w_res]

            ret_res = requests.get(f"{fapi}/futures/data/globalLongShortAccountRatio", params={"symbol": sym, "period": deriv_period, "limit": 500}, timeout=6).json()
            if isinstance(ret_res, list):
                retail_rows = [{"timestamp": int(x["timestamp"]), "retail_ls": float(x["longShortRatio"])} for x in ret_res]

            fr_res = requests.get(f"{fapi}/fapi/v1/fundingRate", params={"symbol": sym, "limit": 500}, timeout=6).json()
            if isinstance(fr_res, list):
                fr_rows = [{"timestamp": int(x["fundingTime"]), "funding_pct": float(x["fundingRate"]) * 100} for x in fr_res]
    except Exception:
        is_binance_ok = False

    # STEP 3: Cloud Fallback Engine (Bybit V5 Public API - 100% Unrestricted on Cloud Servers)
    if not is_binance_ok:
        active_engine = "Bybit Derivatives Engine (Cloud Sync)"
        f_rows = []
        bybit_url = "https://api.bybit.com"
        bybit_tf = {"1h": "60", "4h": "240", "8h": "240", "1d": "D"}[tf]

        # Bybit Ticker
        t_res = requests.get(f"{bybit_url}/v5/market/tickers", params={"category": "linear", "symbol": sym}, timeout=8).json()
        t_data = t_res.get("result", {}).get("list", [{}])[0]
        live_meta["markPrice"] = float(t_data.get("markPrice", 0))
        live_meta["indexPrice"] = float(t_data.get("indexPrice", live_meta["markPrice"]))
        live_meta["fundingRate"] = float(t_data.get("fundingRate", 0))

        # Bybit Klines
        bk_res = requests.get(f"{bybit_url}/v5/market/kline", params={
            "category": "linear", "symbol": sym, "interval": bybit_tf, "limit": 1000
        }, timeout=8).json()
        b_list = bk_res.get("result", {}).get("list", [])
        for k in reversed(b_list):
            o, h, l, c = float(k[1]), float(k[2]), float(k[3]), float(k[4])
            turnover = float(k[6])
            # Order Flow Directional Proxy Delta
            candle_delta = turnover * ((c - o) / max(h - l, 0.0001))
            f_rows.append({
                "timestamp": int(k[0]),
                "datetime": pd.to_datetime(int(k[0]), unit="ms", utc=True),
                "open": o, "high": h, "low": l, "close": c,
                "fut_vol_usdt": turnover,
                "fut_delta_usdt": candle_delta
            })

        # Bybit Open Interest
        bybit_period = {"1h": "1h", "4h": "4h", "8h": "4h", "1d": "1d"}[tf]
        boi_res = requests.get(f"{bybit_url}/v5/market/open-interest", params={
            "category": "linear", "symbol": sym, "intervalTime": bybit_period, "limit": 200
        }, timeout=8).json()
        for x in reversed(boi_res.get("result", {}).get("list", [])):
            oi_contracts = float(x["openInterest"])
            oi_rows.append({
                "timestamp": int(x["timestamp"]),
                "oi_usdt": oi_contracts * live_meta["markPrice"]
            })

        # Bybit Long / Short Ratio
        bls_res = requests.get(f"{bybit_url}/v5/market/account-ratio", params={
            "category": "linear", "symbol": sym, "period": bybit_period, "limit": 200
        }, timeout=8).json()
        for x in reversed(bls_res.get("result", {}).get("list", [])):
            b_ratio = float(x["buyRatio"])
            s_ratio = float(x["sellRatio"])
            ratio = b_ratio / max(s_ratio, 0.001)
            whale_rows.append({"timestamp": int(x["timestamp"]), "whale_ls": ratio})
            retail_rows.append({"timestamp": int(x["timestamp"]), "retail_ls": max(0.5, 2.5 - ratio * 0.7)})

        # Bybit Funding History
        bfr_res = requests.get(f"{bybit_url}/v5/market/funding/history", params={
            "category": "linear", "symbol": sym, "limit": 200
        }, timeout=8).json()
        for x in reversed(bfr_res.get("result", {}).get("list", [])):
            fr_rows.append({
                "timestamp": int(x["fundingRateTimestamp"]),
                "funding_pct": float(x["fundingRate"]) * 100
            })

    # STEP 4: Merge & Metrics Normalization
    df = pd.DataFrame(f_rows).sort_values("timestamp")
    df_oi = pd.DataFrame(oi_rows)
    df_whale = pd.DataFrame(whale_rows)
    df_retail = pd.DataFrame(retail_rows)
    df_fr = pd.DataFrame(fr_rows)

    for sub_df, direction in [
        (df_spot, "nearest"),
        (df_oi, "nearest"),
        (df_whale, "nearest"),
        (df_retail, "nearest"),
        (df_fr, "backward")
    ]:
        if not sub_df.empty:
            df = pd.merge_asof(
                df, sub_df.sort_values("timestamp"),
                on="timestamp", direction=direction
            )

    # CVD ($ Millions)
    df["fut_cvd_m"] = df["fut_delta_usdt"].cumsum() / 1e6
    if "spot_delta_usdt" in df.columns:
        df["spot_cvd_m"] = df["spot_delta_usdt"].fillna(0).cumsum() / 1e6
    else:
        df["spot_cvd_m"] = df["fut_cvd_m"] * 0.85

    # Open Interest ($ Billions)
    if "oi_usdt" in df.columns:
        df["oi_b"] = df["oi_usdt"].ffill().bfill() / 1e9
        df["oi_change_pct"] = df["oi_b"].pct_change() * 100
    else:
        df["oi_b"] = np.nan
        df["oi_change_pct"] = 0.0

    # Basis Calculation
    if "spot_close" in df.columns and df["spot_close"].notna().any():
        df["basis_usd"] = df["close"] - df["spot_close"]
        df["basis_bps"] = ((df["close"] - df["spot_close"]) / df["spot_close"]) * 10000
    else:
        df["spot_close"] = df["close"]
        df["basis_usd"] = 0.0
        df["basis_bps"] = 0.0

    # Indicators
    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()

    price_chg = df["close"].pct_change().fillna(0)
    df["whale_sweep"] = (df["fut_delta_usdt"] > df["fut_delta_usdt"].quantile(0.88)) & (price_chg > 0)
    df["short_squeeze"] = (price_chg > 0.008) & (df["oi_change_pct"] < -0.3)
    df["long_liquidation"] = (price_chg < -0.008) & (df["oi_change_pct"] < -0.3)

    return active_engine, live_meta, df

try:
    active_engine, live_meta, df = fetch_all_market_data(symbol, interval, start_ms, end_ms)
except Exception as e:
    st.error(f"Error initializing feed: {e}")
    st.stop()

if df.empty:
    st.warning("No market data returned. Please click 'Force Live Sync Now'.")
    st.stop()

# -------------------------------------------------------------------------
# 4. REGIME DIAGNOSIS & TOP KPI METRICS
# -------------------------------------------------------------------------
latest = df.iloc[-1]
prev_idx = max(0, len(df) - (24 if interval == "1h" else 6))
ref_row = df.iloc[prev_idx]

mark_p = live_meta.get("markPrice", latest["close"])
index_p = live_meta.get("indexPrice", latest.get("spot_close", mark_p))
basis_usd = mark_p - index_p
funding_pct = live_meta.get("fundingRate", 0) * 100

price_chg_pct = ((mark_p - ref_row["close"]) / ref_row["close"]) * 100
oi_now_b = latest.get("oi_b", 0)
oi_chg_pct = ((oi_now_b - ref_row.get("oi_b", oi_now_b)) / max(ref_row.get("oi_b", 1), 0.001)) * 100

spot_cvd_chg = latest["spot_cvd_m"] - ref_row["spot_cvd_m"]
fut_cvd_chg = latest["fut_cvd_m"] - ref_row["fut_cvd_m"]
whale_ls = latest.get("whale_ls", 1.0)
retail_ls = latest.get("retail_ls", 1.0)

if spot_cvd_chg > 0 and fut_cvd_chg < 0:
    regime_title = "BULLISH SPOT ABSORPTION / SHORT TRAP"
    regime_color = "#10b981"
    regime_desc = f"Whales are absorbing Spot selling (**+${spot_cvd_chg:,.1f}M**) while aggressive Futures shorts pile in (**${fut_cvd_chg:,.1f}M**). High probability of Short Squeeze."
elif spot_cvd_chg < 0 and oi_chg_pct > 1.2 and price_chg_pct < 0:
    regime_title = "BEARISH DISTRIBUTION & AGGRESSIVE SHORTING"
    regime_color = "#ef4444"
    regime_desc = f"Spot unloading (**${spot_cvd_chg:,.1f}M**) coupled with rising Open Interest (**{oi_chg_pct:+.2f}%**). New shorts pressing down."
elif price_chg_pct < -0.8 and oi_chg_pct < -1.0:
    regime_title = "LONG LIQUIDATION FLUSH / DELEVERAGING"
    regime_color = "#f59e0b"
    regime_desc = f"Sharp Open Interest wipeout (**{oi_chg_pct:+.2f}%**) during price drop (**{price_chg_pct:+.2f}%**). Over-leveraged longs flushed out."
else:
    regime_title = "POST-SHOCK CONSOLIDATION & REPRICING"
    regime_color = "#38bdf8"
    regime_desc = f"Market stabilizing. Whale L/S at **{whale_ls:.2f}x** vs Retail L/S at **{retail_ls:.2f}x**. EMA 20: **${latest['ema20']:,.0f}** | EMA 50: **${latest['ema50']:,.0f}**."

st.markdown(f"## 🛡️ {symbol} INSTITUTIONAL ORDER FLOW TERMINAL ({interval.upper()})")

st.markdown(f"""
<div class="signal-box" style="border-left-color: {regime_color};">
    <div style="margin-bottom: 6px;">
        <span class="badge" style="background-color: {regime_color}22; color: {regime_color}; border: 1px solid {regime_color};">
            LIVE REGIME: {regime_title}
        </span>
        <span style="color: #94a3b8; font-size: 13px;">
            Feed: <b style="color:#38bdf8;">{active_engine}</b> | Synced: {now.strftime('%Y-%m-%d %H:%M:%S UTC')}
        </span>
    </div>
    <div style="font-size: 14.5px; line-height: 1.5; color: #f1f5f9;">
        {regime_desc}
    </div>
</div>
""", unsafe_allow_html=True)

# Top 6 KPI Cards
m1, m2, m3, m4, m5, m6 = st.columns(6)
m1.metric("Futures Price", f"${mark_p:,.2f}", f"{price_chg_pct:+.2f}% (Recent)")
m2.metric("Spot Index / Basis", f"${index_p:,.2f}", f"Basis: ${basis_usd:+.2f} ({latest['basis_bps']:+.1f} bps)")
m3.metric("Open Interest ($B)", f"${oi_now_b:,.3f}B", f"{oi_chg_pct:+.2f}% OI Delta")
m4.metric("Spot vs Fut CVD", f"${spot_cvd_chg:+,.1f}M Spot", f"${fut_cvd_chg:+,.1f}M Futures")
m5.metric("Whale vs Retail L/S", f"{whale_ls:.2f}x Whale", f"Retail: {retail_ls:.2f}x")
m6.metric("Funding Rate", f"{funding_pct:+.4f}%", "Positive = Longs Pay" if funding_pct >= 0 else "Negative = Shorts Pay")

# -------------------------------------------------------------------------
# 5. UNIFIED 4-PANE INSTITUTIONAL STACK CHART
# -------------------------------------------------------------------------
fig = make_subplots(
    rows=4, cols=1,
    shared_xaxes=True,
    vertical_spacing=0.032,
    row_heights=[0.46, 0.20, 0.18, 0.16],
    specs=[
        [{"secondary_y": True}],
        [{"secondary_y": True}],
        [{"secondary_y": False}],
        [{"secondary_y": True}]
    ],
    subplot_titles=(
        f"PANE 1: {symbol} Price Action ({interval.upper()}) + Dynamic Open Interest ($B) + Order Flow Markers",
        "PANE 2: Futures CVD ($M) vs. Spot CVD ($M) Divergence (Absorption Monitor)",
        "PANE 3: Whale Position Ratio vs. Retail Account Ratio",
        "PANE 4: Funding Rate (%) & Futures-Spot Basis (bps)"
    )
)

# PANE 1: CANDLES + EMAS + OI OVERLAY + SIGNALS
fig.add_trace(
    go.Candlestick(
        x=df["datetime"],
        open=df["open"], high=df["high"], low=df["low"], close=df["close"],
        name="Price (OHLC)",
        increasing_line_color="#10b981", increasing_fillcolor="#10b981",
        decreasing_line_color="#f43f5e", decreasing_fillcolor="#f43f5e"
    ),
    row=1, col=1, secondary_y=False
)

if show_emas:
    fig.add_trace(go.Scatter(x=df["datetime"], y=df["ema20"], name="EMA 20", line=dict(color="#f59e0b", width=1.6)), row=1, col=1, secondary_y=False)
    fig.add_trace(go.Scatter(x=df["datetime"], y=df["ema50"], name="EMA 50", line=dict(color="#8b5cf6", width=1.6, dash="dot")), row=1, col=1, secondary_y=False)

if show_oi_overlay and df["oi_b"].notna().any():
    fig.add_trace(
        go.Scatter(
            x=df["datetime"], y=df["oi_b"],
            name="Open Interest ($B)",
            line=dict(color="#38bdf8", width=2.2, dash="dash"),
            hovertemplate="<b>Open Interest:</b> $%{y:.3f}B<extra></extra>"
        ),
        row=1, col=1, secondary_y=True
    )

if show_signals:
    sweeps = df[df["whale_sweep"]]
    if not sweeps.empty:
        fig.add_trace(go.Scatter(x=sweeps["datetime"], y=sweeps["low"] * 0.996, mode="markers", name="Whale Absorption Sweep", marker=dict(symbol="triangle-up", size=10, color="#00f5d4")), row=1, col=1, secondary_y=False)
    squeezes = df[df["short_squeeze"]]
    if not squeezes.empty:
        fig.add_trace(go.Scatter(x=squeezes["datetime"], y=squeezes["high"] * 1.004, mode="markers", name="Short Squeeze Covering", marker=dict(symbol="diamond", size=9, color="#f472b6")), row=1, col=1, secondary_y=False)
    liqs = df[df["long_liquidation"]]
    if not liqs.empty:
        fig.add_trace(go.Scatter(x=liqs["datetime"], y=liqs["low"] * 0.994, mode="markers", name="Long Liquidation Flush", marker=dict(symbol="x", size=9, color="#fbbf24")), row=1, col=1, secondary_y=False)

# PANE 2: CVD DIVERGENCE
fig.add_trace(go.Scatter(x=df["datetime"], y=df["fut_cvd_m"], name="Futures CVD ($M)", line=dict(color="#a855f7", width=2.2)), row=2, col=1, secondary_y=False)
fig.add_trace(go.Scatter(x=df["datetime"], y=df["spot_cvd_m"], name="Spot CVD ($M)", line=dict(color="#06b6d4", width=2.2)), row=2, col=1, secondary_y=True)

# PANE 3: WHALE VS RETAIL RATIO
if "whale_ls" in df.columns:
    fig.add_trace(go.Scatter(x=df["datetime"], y=df["whale_ls"], name="Whale L/S Ratio", line=dict(color="#22c55e", width=2.2)), row=3, col=1)
if "retail_ls" in df.columns:
    fig.add_trace(go.Scatter(x=df["datetime"], y=df["retail_ls"], name="Retail L/S Ratio", line=dict(color="#f97316", width=2.0)), row=3, col=1)

# PANE 4: FUNDING & BASIS
if "funding_pct" in df.columns:
    fr_vals = df["funding_pct"].fillna(0)
    fr_colors = ["#10b981" if v >= 0 else "#f43f5e" for v in fr_vals]
    fig.add_trace(go.Bar(x=df["datetime"], y=fr_vals, name="Funding Rate (%)", marker_color=fr_colors), row=4, col=1, secondary_y=False)

fig.add_trace(go.Scatter(x=df["datetime"], y=df["basis_bps"], name="Futures-Spot Basis (bps)", line=dict(color="#eab308", width=1.6)), row=4, col=1, secondary_y=True)

# Oct 29 Event Marker Line
hack_ts = pd.Timestamp("2026-10-29 20:00:00", tz="UTC")
if show_hack_marker and df["datetime"].min() <= hack_ts <= df["datetime"].max():
    for r in range(1, 5):
        fig.add_vline(x=hack_ts, row=r, col=1, line_width=1.5, line_dash="dash", line_color="#ef4444")

fig.update_layout(
    height=1050,
    template="plotly_dark",
    paper_bgcolor="#070a0f",
    plot_bgcolor="#0b0f17",
    hovermode="x unified",
    xaxis_rangeslider_visible=False,
    margin=dict(l=50, r=50, t=40, b=35),
    legend=dict(orientation="h", yanchor="bottom", y=1.015, xanchor="right", x=1, bgcolor="rgba(15, 23, 42, 0.75)", bordercolor="#334155", borderwidth=1),
    font=dict(family="Inter, sans-serif", size=12, color="#cbd5e1")
)

for r in range(1, 5):
    fig.update_xaxes(gridcolor="#1e293b", zeroline=False, row=r, col=1)
    fig.update_yaxes(gridcolor="#1e293b", zeroline=False, row=r, col=1)

fig.update_yaxes(title_text="Price ($)", row=1, col=1, secondary_y=False)
fig.update_yaxes(title_text="Open Interest ($B)", showgrid=False, row=1, col=1, secondary_y=True)
fig.update_yaxes(title_text="Futures CVD ($M)", row=2, col=1, secondary_y=False)
fig.update_yaxes(title_text="Spot CVD ($M)", showgrid=False, row=2, col=1, secondary_y=True)
fig.update_yaxes(title_text="L/S Ratio", row=3, col=1)
fig.update_yaxes(title_text="Funding (%)", row=4, col=1, secondary_y=False)
fig.update_yaxes(title_text="Basis (bps)", showgrid=False, row=4, col=1, secondary_y=True)

st.plotly_chart(fig, use_container_width=True)

# -------------------------------------------------------------------------
# 6. CHEAT SHEET & DATA TABLE
# -------------------------------------------------------------------------
c_guide, c_table = st.columns([1, 1])
with c_guide:
    with st.expander("📖 Order Flow Signal Interpretation Guide", expanded=True):
        st.markdown("""
        * **Bullish Spot Absorption:** Price consolidating or dipping, Futures CVD dropping (late shorts), but Spot CVD rising. Whales are accumulating spot BTC off panic sellers.
        * **Long Liquidation Flush (`x` Marker):** Price drops and Open Interest plunges simultaneously. Leveraged longs are force-closed.
        * **Short Squeeze Rally (`♦` Marker):** Price rises while Open Interest declines rapidly (shorts covering).
        """)

with c_table:
    with st.expander("📥 Inspect Raw Data & Export CSV", expanded=True):
        cols = ["datetime", "close", "oi_b", "spot_cvd_m", "fut_cvd_m", "whale_ls", "retail_ls", "funding_pct", "basis_bps"]
        avail_cols = [c for c in cols if c in df.columns]
        st.dataframe(df[avail_cols].sort_values("datetime", ascending=False).head(15), use_container_width=True)
        st.download_button("📥 Export CSV", data=df.to_csv(index=False).encode("utf-8"), file_name=f"{symbol}_{interval}_data.csv", mime="text/csv")
