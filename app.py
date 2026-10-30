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
    page_title="BTCUSDT Post-Hack Institutional Order Flow Terminal",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded"
)

st.markdown("""
<style>
    /* Force Dark Terminal Theme Across All Containers */
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
    /* Metric Cards Styling */
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
    /* Diagnostic Signal Box */
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
symbol = st.sidebar.selectbox("Asset Pair", ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"], index=0)
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
# 3. DATA ENGINE (FUTURES + SPOT + OI + L/S + FUNDING + BASIS)
# -------------------------------------------------------------------------
now = datetime.datetime.now(datetime.timezone.utc)
start_dt = now - datetime.timedelta(days=days_back)
start_ms = int(start_dt.timestamp() * 1000)
end_ms = int(now.timestamp() * 1000)

# Map 8h kline timeframe to 4h derivatives period for seamless alignment
deriv_period_map = {"1h": "1h", "4h": "4h", "8h": "4h", "1d": "1d"}
deriv_period = deriv_period_map[interval]

@st.cache_data(ttl=12)
def fetch_all_market_data(sym, tf, d_period, start_time, end_time):
    fapi_url = "https://fapi.binance.com"
    spot_url = "https://api.binance.com"

    # 1. Live Premium Index (Mark Price, Index/Spot Price, Funding)
    prem_res = requests.get(f"{fapi_url}/fapi/v1/premiumIndex", params={"symbol": sym}, timeout=10).json()

    # 2. Futures Klines (OHLC, Futures Volume, Futures Taker Buy Volume)
    fk_res = requests.get(f"{fapi_url}/fapi/v1/klines", params={
        "symbol": sym, "interval": tf, "startTime": start_time, "endTime": end_time, "limit": 1000
    }, timeout=10).json()

    f_rows = []
    for k in fk_res:
        vol_usdt = float(k[7])
        taker_buy = float(k[10])
        taker_sell = vol_usdt - taker_buy
        f_delta = taker_buy - taker_sell
        f_rows.append({
            "timestamp": int(k[0]),
            "datetime": pd.to_datetime(k[0], unit="ms", utc=True),
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "fut_vol_usdt": vol_usdt,
            "fut_delta_usdt": f_delta,
            "trades": int(k[8])
        })
    df_fut = pd.DataFrame(f_rows)

    # 3. Spot Klines (To compute Spot CVD & Futures-Spot Basis)
    sk_res = requests.get(f"{spot_url}/api/v3/klines", params={
        "symbol": sym, "interval": tf, "startTime": start_time, "endTime": end_time, "limit": 1000
    }, timeout=10).json()

    s_rows = []
    for k in sk_res:
        s_vol = float(k[7])
        s_buy = float(k[10])
        s_sell = s_vol - s_buy
        s_delta = s_buy - s_sell
        s_rows.append({
            "timestamp": int(k[0]),
            "spot_close": float(k[4]),
            "spot_vol_usdt": s_vol,
            "spot_delta_usdt": s_delta
        })
    df_spot = pd.DataFrame(s_rows)

    # 4. Open Interest History
    oi_res = requests.get(f"{fapi_url}/futures/data/openInterestHist", params={
        "symbol": sym, "period": d_period, "startTime": start_time, "endTime": end_time, "limit": 500
    }, timeout=10).json()
    df_oi = pd.DataFrame([
        {
            "timestamp": int(x["timestamp"]),
            "oi_usdt": float(x["sumOpenInterestValue"]),
            "oi_contracts": float(x["sumOpenInterest"])
        } for x in oi_res
    ]) if isinstance(oi_res, list) and len(oi_res) > 0 else pd.DataFrame()

    # 5. Top Trader Long/Short Position Ratio (Whales)
    whale_res = requests.get(f"{fapi_url}/futures/data/topLongShortPositionRatio", params={
        "symbol": sym, "period": d_period, "startTime": start_time, "endTime": end_time, "limit": 500
    }, timeout=10).json()
    df_whale = pd.DataFrame([
        {
            "timestamp": int(x["timestamp"]),
            "whale_ls": float(x["longShortRatio"]),
            "whale_long_pct": float(x["longAccount"]) * 100
        } for x in whale_res
    ]) if isinstance(whale_res, list) and len(whale_res) > 0 else pd.DataFrame()

    # 6. Global Account Long/Short Ratio (Retail)
    retail_res = requests.get(f"{fapi_url}/futures/data/globalLongShortAccountRatio", params={
        "symbol": sym, "period": d_period, "startTime": start_time, "endTime": end_time, "limit": 500
    }, timeout=10).json()
    df_retail = pd.DataFrame([
        {
            "timestamp": int(x["timestamp"]),
            "retail_ls": float(x["longShortRatio"])
        } for x in retail_res
    ]) if isinstance(retail_res, list) and len(retail_res) > 0 else pd.DataFrame()

    # 7. Historical Funding Rates
    fr_res = requests.get(f"{fapi_url}/fapi/v1/fundingRate", params={
        "symbol": sym, "startTime": start_time, "endTime": end_time, "limit": 1000
    }, timeout=10).json()
    df_fr = pd.DataFrame([
        {
            "timestamp": int(x["fundingTime"]),
            "funding_pct": float(x["fundingRate"]) * 100
        } for x in fr_res
    ]) if isinstance(fr_res, list) and len(fr_res) > 0 else pd.DataFrame()

    # --- MERGE & COMPUTE INSTITUTIONAL METRICS ---
    df = df_fut.sort_values("timestamp")
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

    # Cumulative Volume Delta (CVD) in $ Millions
    df["fut_cvd_m"] = df["fut_delta_usdt"].cumsum() / 1e6
    if "spot_delta_usdt" in df.columns:
        df["spot_cvd_m"] = df["spot_delta_usdt"].fillna(0).cumsum() / 1e6
    else:
        df["spot_cvd_m"] = 0.0

    # Open Interest in $ Billions & OI Delta
    if "oi_usdt" in df.columns:
        df["oi_b"] = df["oi_usdt"].ffill().bfill() / 1e9
        df["oi_change_pct"] = df["oi_b"].pct_change() * 100
    else:
        df["oi_b"] = np.nan
        df["oi_change_pct"] = 0.0

    # Futures vs Spot Basis (in USD and bps)
    if "spot_close" in df.columns:
        df["basis_usd"] = df["close"] - df["spot_close"]
        df["basis_bps"] = ((df["close"] - df["spot_close"]) / df["spot_close"]) * 10000
    else:
        df["basis_usd"] = 0.0
        df["basis_bps"] = 0.0

    # EMAs
    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()

    # Signal Detection
    price_chg = df["close"].pct_change().fillna(0)
    df["whale_sweep"] = (df["spot_delta_usdt"] > df["spot_delta_usdt"].quantile(0.90)) & (price_chg > 0)
    df["short_squeeze"] = (price_chg > 0.008) & (df["oi_change_pct"] < -0.5)
    df["long_liquidation"] = (price_chg < -0.008) & (df["oi_change_pct"] < -0.5)

    return prem_res, df

try:
    live_prem, df = fetch_all_market_data(symbol, interval, deriv_period, start_ms, end_ms)
except Exception as e:
    st.error(f"Error connecting to Binance API: {e}")
    st.stop()

if df.empty:
    st.warning("No data returned from Binance API for the selected parameters.")
    st.stop()

# -------------------------------------------------------------------------
# 4. AUTOMATED MARKET INTELLIGENCE & SIGNAL READER
# -------------------------------------------------------------------------
latest = df.iloc[-1]
prev_24h_idx = max(0, len(df) - (24 if interval == "1h" else (6 if interval == "4h" else (3 if interval == "8h" else 2))))
ref_row = df.iloc[prev_24h_idx]

mark_price = float(live_prem.get("markPrice", latest["close"]))
index_price = float(live_prem.get("indexPrice", latest.get("spot_close", mark_price)))
live_basis_usd = mark_price - index_price
live_funding_pct = float(live_prem.get("lastFundingRate", 0)) * 100

price_chg_pct = ((mark_price - ref_row["close"]) / ref_row["close"]) * 100
oi_now_b = latest.get("oi_b", 0)
oi_chg_pct = ((oi_now_b - ref_row.get("oi_b", oi_now_b)) / max(ref_row.get("oi_b", 1), 0.001)) * 100

spot_cvd_chg = latest["spot_cvd_m"] - ref_row["spot_cvd_m"]
fut_cvd_chg = latest["fut_cvd_m"] - ref_row["fut_cvd_m"]
whale_ls_now = latest.get("whale_ls", 1.0)
retail_ls_now = latest.get("retail_ls", 1.0)

# Determine Market Regime & Actionable Diagnosis
if spot_cvd_chg > 0 and fut_cvd_chg < 0:
    regime_title = "BULLISH SPOT ABSORPTION / SHORT TRAP SETUP"
    regime_color = "#10b981"
    regime_desc = (
        f"Spot CVD increased by **+${spot_cvd_chg:,.1f}M** while Futures traders aggressively sold (**${fut_cvd_chg:,.1f}M**). "
        "Smart money is absorbing the panic selling on Spot while late Futures shorts pile in—high probability of a Short Squeeze if price reclaims EMA 20."
    )
elif spot_cvd_chg < 0 and oi_chg_pct > 1.5 and price_chg_pct < 0:
    regime_title = "BEARISH DISTRIBUTION & AGGRESSIVE SHORTING"
    regime_color = "#ef4444"
    regime_desc = (
        f"Spot holders are unloading (**${spot_cvd_chg:,.1f}M**) while Open Interest is rising (**{oi_chg_pct:+.2f}%**). "
        "New shorts are pressing the breakdown alongside real spot outflows. Avoid catching falling knives until OI flushes out."
    )
elif price_chg_pct < -1.0 and oi_chg_pct < -1.5:
    regime_title = "LONG LIQUIDATION FLUSH / DELEVERAGING"
    regime_color = "#f59e0b"
    regime_desc = (
        f"Price dropped (**{price_chg_pct:+.2f}%**) accompanied by a sharp Open Interest wipeout (**{oi_chg_pct:+.2f}%**). "
        "Over-leveraged longs have been force-liquidated. Watch for Spot CVD stabilization to confirm a local bottom."
    )
else:
    regime_title = "POST-SHOCK CONSOLIDATION & REPRICING"
    regime_color = "#38bdf8"
    regime_desc = (
        f"Market is stabilizing with Whale L/S at **{whale_ls_now:.2f}** vs Retail L/S at **{retail_ls_now:.2f}**. "
        f"Live Basis is **${live_basis_usd:+.2f}** and Funding is **{live_funding_pct:+.4f}%**. Watch for a breakout beyond EMA 20 (${latest['ema20']:,.0f}) or EMA 50 (${latest['ema50']:,.0f})."
    )

st.markdown(f"## 🛡️ {symbol} INSTITUTIONAL ORDER FLOW & POST-HACK MONITOR ({interval.upper()})")

st.markdown(f"""
<div class="signal-box" style="border-left-color: {regime_color};">
    <div style="margin-bottom: 6px;">
        <span class="badge" style="background-color: {regime_color}22; color: {regime_color}; border: 1px solid {regime_color};">
            LIVE REGIME: {regime_title}
        </span>
        <span style="color: #94a3b8; font-size: 13px;">
            Synced: {now.strftime('%Y-%m-%d %H:%M:%S UTC')} | Timeframe: {interval.upper()}
        </span>
    </div>
    <div style="font-size: 14.5px; line-height: 1.5; color: #f1f5f9;">
        {regime_desc}
    </div>
</div>
""", unsafe_allow_html=True)

# Top 6 KPI Metrics
m1, m2, m3, m4, m5, m6 = st.columns(6)
m1.metric("Futures Mark Price", f"${mark_price:,.2f}", f"{price_chg_pct:+.2f}% (Recent)")
m2.metric("Spot Index / Basis", f"${index_price:,.2f}", f"Basis: ${live_basis_usd:+.2f} ({latest['basis_bps']:+.1f} bps)")
m3.metric("Open Interest ($B)", f"${oi_now_b:,.3f}B", f"{oi_chg_pct:+.2f}% OI Delta")
m4.metric("Spot vs Fut CVD Delta", f"${spot_cvd_chg:+,.1f}M Spot", f"${fut_cvd_chg:+,.1f}M Futures")
m5.metric("Whale vs Retail L/S", f"{whale_ls_now:.2f}x Whale", f"Retail: {retail_ls_now:.2f}x")
m6.metric("Live Funding Rate", f"{live_funding_pct:+.4f}%", "Positive = Longs Pay" if live_funding_pct >= 0 else "Negative = Shorts Pay")

# -------------------------------------------------------------------------
# 5. UNIFIED 4-PANE INSTITUTIONAL STACK CHART
# -------------------------------------------------------------------------
fig = make_subplots(
    rows=4, cols=1,
    shared_xaxes=True,
    vertical_spacing=0.032,
    row_heights=[0.46, 0.20, 0.18, 0.16],
    specs=[
        [{"secondary_y": True}],   # Pane 1: Price Candles + EMAs + Dynamic Open Interest ($B) + Signals
        [{"secondary_y": True}],   # Pane 2: Futures CVD ($M) vs Spot CVD ($M) Divergence
        [{"secondary_y": False}],  # Pane 3: Whale L/S (Top 20%) vs Retail L/S Positioning
        [{"secondary_y": True}]    # Pane 4: Funding Rate (%) + Futures-Spot Basis (bps)
    ],
    subplot_titles=(
        f"PANE 1: {symbol} Price Action ({interval.upper()}) + EMAs + Dynamic Open Interest ($B) + Order Flow Signals",
        "PANE 2: Futures CVD ($M) vs. Spot CVD ($M) Divergence (Spot Absorption vs. Unloading)",
        "PANE 3: Whale L/S Ratio (Top 20% Positions) vs. Retail L/S Account Ratio",
        "PANE 4: Funding Rate (%) & Futures-Spot Basis Premium/Discount (bps)"
    )
)

# --- PANE 1: PRICE CANDLESTICKS + EMAs + DYNAMIC OPEN INTEREST ---
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
    fig.add_trace(
        go.Scatter(
            x=df["datetime"], y=df["ema20"],
            name="EMA 20", line=dict(color="#f59e0b", width=1.6)
        ),
        row=1, col=1, secondary_y=False
    )
    fig.add_trace(
        go.Scatter(
            x=df["datetime"], y=df["ema50"],
            name="EMA 50", line=dict(color="#8b5cf6", width=1.6, dash="dot")
        ),
        row=1, col=1, secondary_y=False
    )

# Dynamic Open Interest (Auto-scaled on Secondary Y-Axis)
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

# Signal Markers (Whale Sweeps, Short Squeezes, Long Liquidations)
if show_signals:
    sweeps = df[df["whale_sweep"]]
    if not sweeps.empty:
        fig.add_trace(
            go.Scatter(
                x=sweeps["datetime"], y=sweeps["low"] * 0.996,
                mode="markers", name="Spot Whale Sweep",
                marker=dict(symbol="triangle-up", size=10, color="#00f5d4", line=dict(width=1, color="#000"))
            ),
            row=1, col=1, secondary_y=False
        )
    squeezes = df[df["short_squeeze"]]
    if not squeezes.empty:
        fig.add_trace(
            go.Scatter(
                x=squeezes["datetime"], y=squeezes["high"] * 1.004,
                mode="markers", name="Short Squeeze / Covering",
                marker=dict(symbol="diamond", size=9, color="#f472b6", line=dict(width=1, color="#000"))
            ),
            row=1, col=1, secondary_y=False
        )
    liqs = df[df["long_liquidation"]]
    if not liqs.empty:
        fig.add_trace(
            go.Scatter(
                x=liqs["datetime"], y=liqs["low"] * 0.994,
                mode="markers", name="Long Liquidation Flush",
                marker=dict(symbol="x", size=9, color="#fbbf24")
            ),
            row=1, col=1, secondary_y=False
        )

# --- PANE 2: FUTURES CVD vs SPOT CVD DIVERGENCE ---
fig.add_trace(
    go.Scatter(
        x=df["datetime"], y=df["fut_cvd_m"],
        name="Futures CVD ($M)",
        line=dict(color="#a855f7", width=2.2),
        hovertemplate="<b>Futures CVD:</b> $%{y:,.1f}M<extra></extra>"
    ),
    row=2, col=1, secondary_y=False
)

fig.add_trace(
    go.Scatter(
        x=df["datetime"], y=df["spot_cvd_m"],
        name="Spot CVD ($M)",
        line=dict(color="#06b6d4", width=2.2),
        hovertemplate="<b>Spot CVD:</b> $%{y:,.1f}M<extra></extra>"
    ),
    row=2, col=1, secondary_y=True
)

# --- PANE 3: WHALE L/S vs RETAIL L/S RATIO ---
if "whale_ls" in df.columns:
    fig.add_trace(
        go.Scatter(
            x=df["datetime"], y=df["whale_ls"],
            name="Whale L/S (Top 20%)",
            line=dict(color="#22c55e", width=2.2),
            hovertemplate="<b>Whale L/S:</b> %{y:.2f}x<extra></extra>"
        ),
        row=3, col=1
    )

if "retail_ls" in df.columns:
    fig.add_trace(
        go.Scatter(
            x=df["datetime"], y=df["retail_ls"],
            name="Retail L/S (Global Accounts)",
            line=dict(color="#f97316", width=2.0),
            hovertemplate="<b>Retail L/S:</b> %{y:.2f}x<extra></extra>"
        ),
        row=3, col=1
    )

# --- PANE 4: FUNDING RATE (%) & FUTURES-SPOT BASIS (bps) ---
if "funding_pct" in df.columns:
    fr_vals = df["funding_pct"].fillna(0)
    fr_colors = ["#10b981" if v >= 0 else "#f43f5e" for v in fr_vals]
    fig.add_trace(
        go.Bar(
            x=df["datetime"], y=fr_vals,
            name="Funding Rate (%)",
            marker_color=fr_colors,
            hovertemplate="<b>Funding Rate:</b> %{y:+.4f}%<extra></extra>"
        ),
        row=4, col=1, secondary_y=False
    )

fig.add_trace(
    go.Scatter(
        x=df["datetime"], y=df["basis_bps"],
        name="Futures-Spot Basis (bps)",
        line=dict(color="#eab308", width=1.6),
        hovertemplate="<b>Basis:</b> %{y:+.1f} bps<extra></extra>"
    ),
    row=4, col=1, secondary_y=True
)

# Mark October 29, 2026 20:00 UTC Event Line
hack_ts = pd.Timestamp("2026-10-29 20:00:00", tz="UTC")
if show_hack_marker and df["datetime"].min() <= hack_ts <= df["datetime"].max():
    for r in range(1, 5):
        fig.add_vline(
            x=hack_ts, row=r, col=1,
            line_width=1.5, line_dash="dash", line_color="#ef4444"
        )
    fig.add_annotation(
        x=hack_ts, y=df["high"].max(),
        row=1, col=1,
        text="⚠️ Oct 29 Event Shock",
        showarrow=True, arrowhead=2, arrowcolor="#ef4444",
        font=dict(color="#fecaca", size=11),
        bgcolor="#7f1d1d", bordercolor="#ef4444"
    )

# Layout & Institutional Dark Styling
fig.update_layout(
    height=1050,
    template="plotly_dark",
    paper_bgcolor="#070a0f",
    plot_bgcolor="#0b0f17",
    hovermode="x unified",
    xaxis_rangeslider_visible=False,
    margin=dict(l=50, r=50, t=40, b=35),
    legend=dict(
        orientation="h", yanchor="bottom", y=1.015, xanchor="right", x=1,
        bgcolor="rgba(15, 23, 42, 0.75)", bordercolor="#334155", borderwidth=1
    ),
    font=dict(family="Inter, sans-serif", size=12, color="#cbd5e1")
)

# Gridlines and Axis Titles
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
# 6. HOW TO READ THIS TERMINAL (QUICK DECISION CHEAT-SHEET) & CSV EXPORT
# -------------------------------------------------------------------------
c_guide, c_table = st.columns([1, 1])

with c_guide:
    with st.expander("📖 How to Read Order Flow Signals (Cheat Sheet)", expanded=True):
        st.markdown("""
        * **1. Spot CVD vs. Futures CVD (Pane 2):**
          * **Bullish Absorption:** Price drops or goes sideways, Futures CVD drops sharply (panic shorting), **but Spot CVD rises or stays flat**. Whales are buying actual spot BTC off panic sellers.
          * **Real Capitulation:** Both Spot CVD and Futures CVD plunge together while Open Interest expands.
        * **2. Price vs. Open Interest (Pane 1):**
          * **Price Down + OI Down (`x` Yellow Marker):** Longs are getting force-liquidated. Once OI stops falling, selling exhaustion sets in.
          * **Price Up + OI Down (`♦` Pink Marker):** Short covering rally (Short Squeeze).
        * **3. Futures-Spot Basis & Funding (Pane 4):**
          * Deeply negative Basis (`bps < -10`) + Negative Funding means perpetual futures are trading at a steep discount to Spot—prime fuel for a violent short squeeze.
        """)

with c_table:
    with st.expander("📥 Inspect Raw Multi-Factor Data & Export CSV", expanded=True):
        cols = ["datetime", "close", "oi_b", "spot_cvd_m", "fut_cvd_m", "whale_ls", "retail_ls", "funding_pct", "basis_bps"]
        avail_cols = [c for c in cols if c in df.columns]
        st.dataframe(df[avail_cols].sort_values("datetime", ascending=False).head(15), use_container_width=True)
        st.download_button(
            "📥 Download Full Multi-Factor CSV",
            data=df.to_csv(index=False).encode("utf-8"),
            file_name=f"{symbol}_{interval}_orderflow.csv",
            mime="text/csv"
        )