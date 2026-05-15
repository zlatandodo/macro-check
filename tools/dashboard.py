"""
tools/dashboard.py — Dashboard macro interattiva (Streamlit)

Avvio:
    venv/bin/streamlit run tools/dashboard.py

Clicca "Aggiorna dati" nella sidebar per ri-fetchare tutto.
"""

import os
import sys
from pathlib import Path

# Rende importabili le funzioni da macro_check.py nella root
sys.path.insert(0, str(Path(__file__).parent.parent))

import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime

# Inietta la FRED API key dall'ambiente giusto:
# - in locale: da .env (caricato da macro_check via load_dotenv)
# - su Streamlit Cloud: da st.secrets
try:
    if "FRED_API_KEY" in st.secrets and not os.environ.get("FRED_API_KEY"):
        os.environ["FRED_API_KEY"] = st.secrets["FRED_API_KEY"]
except Exception:
    pass

from macro_check import (
    fetch_fred, fetch_yfinance_batch, fetch_naaim, fetch_putcall_cboe,
    fetch_ism_prices, fetch_cftc_historical, compute_yoy, compute_sector_metrics,
    compute_cot_zscores, classify_cycle_phase, get_positioning_recommendation,
    FRED_INDICATORS, ASSETS, SECTORS, COT_CONTRACTS,
)

# =============================================================================
# CONFIG PAGINA
# =============================================================================

st.set_page_config(
    page_title="AskDodo",
    page_icon="🦤",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# SVG logo: Dodo con grafico stock stile Wall Street
DODO_LOGO_SVG = """
<svg width="72" height="72" viewBox="0 0 72 72" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <radialGradient id="bgd" cx="38%" cy="35%" r="65%">
      <stop offset="0%" stop-color="#1a3a5c"/>
      <stop offset="100%" stop-color="#0a1520"/>
    </radialGradient>
  </defs>

  <!-- Cerchio sfondo -->
  <circle cx="36" cy="36" r="35" fill="url(#bgd)" stroke="#2e86c1" stroke-width="1.5"/>

  <!-- Grafico stock (linea verde, area fill) -->
  <polyline
    points="5,58 11,50 17,54 23,41 29,46 35,32 41,37 47,22 54,27 67,11"
    fill="none" stroke="#2ecc71" stroke-width="1.8"
    stroke-linecap="round" stroke-linejoin="round" opacity="0.85"/>
  <polygon
    points="5,58 11,50 17,54 23,41 29,46 35,32 41,37 47,22 54,27 67,11 67,64 5,64"
    fill="#2ecc71" opacity="0.07"/>

  <!-- Dodo: coda (pennacchio) -->
  <path d="M21,46 Q10,40 13,51 Q8,46 12,58 Q16,50 22,54"
        fill="#1f6fa3" stroke="#2e86c1" stroke-width="0.5"/>

  <!-- Dodo: corpo -->
  <ellipse cx="38" cy="48" rx="16" ry="12" fill="#2e86c1"/>

  <!-- Dodo: collo + testa (uniti in un path) -->
  <path d="M44,36 Q48,30 50,26 Q46,22 42,22 Q38,22 36,26 Q34,30 36,36 Q38,42 44,36 Z"
        fill="#2e86c1"/>

  <!-- Dodo: ala vestigiale (piccola) -->
  <ellipse cx="28" cy="46" rx="6" ry="3.5" fill="#1a5f8a"
           transform="rotate(-15 28 46)"/>

  <!-- Dodo: becco (uncinato, caratteristico) -->
  <path d="M50,26 Q60,22 58,29 Q62,32 52,31 Z" fill="#f0b429"/>
  <path d="M57,27 Q62,27 59,32" fill="none" stroke="#c89020" stroke-width="1.3"/>

  <!-- Dodo: occhio -->
  <circle cx="48" cy="25" r="3" fill="white"/>
  <circle cx="48.7" cy="25" r="1.5" fill="#0a1520"/>
  <circle cx="49.3" cy="24.3" r="0.6" fill="white"/>

  <!-- Dodo: zampe -->
  <line x1="33" y1="60" x2="30" y2="69" stroke="#f0b429" stroke-width="2.8" stroke-linecap="round"/>
  <line x1="41" y1="60" x2="44" y2="69" stroke="#f0b429" stroke-width="2.8" stroke-linecap="round"/>
  <!-- Dita -->
  <line x1="30" y1="69" x2="23" y2="71" stroke="#f0b429" stroke-width="2" stroke-linecap="round"/>
  <line x1="30" y1="69" x2="31" y2="72" stroke="#f0b429" stroke-width="2" stroke-linecap="round"/>
  <line x1="44" y1="69" x2="50" y2="71" stroke="#f0b429" stroke-width="2" stroke-linecap="round"/>
  <line x1="44" y1="69" x2="45" y2="72" stroke="#f0b429" stroke-width="2" stroke-linecap="round"/>
</svg>
"""

st.markdown("""
<style>
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    [data-testid="stSidebar"] {display: none;}
    [data-testid="collapsedControl"] {display: none;}
    .block-container {padding-top: 1rem; padding-bottom: 2rem;}
    .metric-label {font-size: 0.8rem !important;}
</style>
""", unsafe_allow_html=True)

PHASE_COLORS = {
    "goldilocks":   "#2ecc71",
    "reflation":    "#3498db",
    "stagflation":  "#e74c3c",
    "late_cycle":   "#f39c12",
    "disinflation": "#9b59b6",
    "transition":   "#95a5a6",
}

# =============================================================================
# FETCH CON CACHE — invalidata dal bottone Aggiorna
# =============================================================================

@st.cache_data(show_spinner=False)
def load_main_data(refresh_token: int):
    """Carica dati veloci: FRED, prezzi, NAAIM, CBOE, ISM, settori (~10-15s)."""
    fred_data, prices_summary, prices_raw = {}, {}, pd.DataFrame()
    naaim_df, putcall = pd.DataFrame(), {"total": None, "equity": None}
    ism_df = pd.DataFrame()
    spy_close = pd.Series(dtype=float)
    errors = []

    # FRED
    for code in FRED_INDICATORS:
        try:
            fred_data[code] = fetch_fred(code)
        except Exception as e:
            errors.append(f"FRED {code}: {e}")

    # Prezzi yfinance
    all_tickers = list(ASSETS.keys()) + list(SECTORS.keys()) + ["SPY"]
    try:
        prices_raw = fetch_yfinance_batch(all_tickers, period="1y")
        for ticker in all_tickers:
            if ticker in prices_raw.columns.get_level_values(0):
                s = prices_raw[ticker]["Close"].dropna()
                if not s.empty:
                    last = float(s.iloc[-1])
                    d1m = float(s.iloc[-22]) if len(s) >= 22 else None
                    d3m = float(s.iloc[-66]) if len(s) >= 66 else None
                    change_1m = ((last / d1m - 1) * 100) if d1m else None
                    change_3m = ((last / d3m - 1) * 100) if d3m else None
                    ytd_s = s[s.index.year == s.index[-1].year]
                    ytd = ((last / float(ytd_s.iloc[0]) - 1) * 100) if len(ytd_s) > 0 else None
                    prices_summary[ticker] = {
                        "close": last, "change_1m": change_1m,
                        "change_3m": change_3m, "ytd": ytd,
                        "date": s.index[-1].strftime("%d-%m-%Y"),
                    }
        if "SPY" in prices_raw.columns.get_level_values(0):
            spy_close = prices_raw["SPY"]["Close"].dropna()
    except Exception as e:
        errors.append(f"yfinance: {e}")

    # NAAIM
    try:
        naaim_df = fetch_naaim()
    except Exception as e:
        errors.append(f"NAAIM: {e}")

    # Put/Call CBOE
    try:
        putcall = fetch_putcall_cboe()
    except Exception as e:
        errors.append(f"Put/Call CBOE: {e}")

    # ISM Manufacturing Prices Paid (Trading Economics)
    try:
        ism_df = fetch_ism_prices()
    except Exception as e:
        errors.append(f"ISM Prices: {e}")

    # Sector metrics
    sector_df = pd.DataFrame()
    try:
        sector_df = compute_sector_metrics(prices_raw, spy_close)
    except Exception as e:
        errors.append(f"Sectors: {e}")

    return fred_data, prices_summary, prices_raw, naaim_df, putcall, sector_df, ism_df, errors


@st.cache_data(show_spinner=False)
def load_cot_data(cot_token: int):
    """
    Carica 5 anni di dati COT (CFTC) e calcola z-score per ogni contratto.
    Prima esecuzione: ~60-120s (download ZIP per ogni anno, poi cache giornaliera).
    Esecuzioni successive nella stessa giornata: pochi secondi (lettura CSV cached).
    """
    try:
        hist_data = fetch_cftc_historical(years=5)
        cot_df = compute_cot_zscores(hist_data)
        return cot_df, None
    except Exception as e:
        return pd.DataFrame(), str(e)


# =============================================================================
# HELPERS DI RENDERING
# =============================================================================

def delta_arrow(v):
    if v is None or pd.isna(v):
        return ""
    return "▲" if v > 0 else "▼"


def fmt_pct(v, decimals=1):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    sign = "+" if v > 0 else ""
    return f"{sign}{v:.{decimals}f}%"


def color_pct(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    sign = "+" if v > 0 else ""
    color = "green" if v > 0 else "red"
    return f":{color}[{sign}{v:.1f}%]"


def render_phase_banner(phase: str, phase_desc: str):
    color = PHASE_COLORS.get(phase, "#95a5a6")
    label = phase.upper().replace("_", " ")
    st.markdown(
        f"""
        <div style="
            background-color:{color}22;
            border-left: 6px solid {color};
            border-radius: 6px;
            padding: 14px 20px;
            margin-bottom: 1rem;
        ">
            <span style="font-size:1.4rem; font-weight:700; color:{color};">{label}</span>
            <span style="font-size:1rem; color:#ccc; margin-left:1rem;">{phase_desc}</span>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_kpi_row(fred_data: dict, prices_summary: dict, ism_last=None, ism_prev=None, ism_date="—"):
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    # VIX
    vix = prices_summary.get("^VIX", {})
    c1.metric("VIX", f"{vix.get('close', 0):.1f}" if vix.get("close") else "—",
              fmt_pct(vix.get("change_1m")) if vix.get("change_1m") else None,
              delta_color="inverse")
    # DXY
    dxy = prices_summary.get("DX-Y.NYB", {})
    c2.metric("DXY", f"{dxy.get('close', 0):.2f}" if dxy.get("close") else "—",
              fmt_pct(dxy.get("change_1m")) if dxy.get("change_1m") else None,
              delta_color="off")
    # Curva 10Y-2Y
    df_curve = fred_data.get("T10Y2Y", pd.DataFrame())
    if not df_curve.empty:
        v = df_curve["value"].iloc[-1]
        prev = df_curve["value"].iloc[-2]
        c3.metric("Curva 10Y-2Y", f"{v:.2f}%", f"{v - prev:+.2f}",
                  delta_color="normal" if v > 0 else "inverse")
    else:
        c3.metric("Curva 10Y-2Y", "—")
    # Fed Funds
    df_ff = fred_data.get("DFF", pd.DataFrame())
    if not df_ff.empty:
        v = df_ff["value"].iloc[-1]
        prev = df_ff["value"].iloc[-2]
        c4.metric("Fed Funds", f"{v:.2f}%", f"{v - prev:+.2f}", delta_color="inverse")
    else:
        c4.metric("Fed Funds", "—")
    # Treasury 10Y
    df_10y = fred_data.get("DGS10", pd.DataFrame())
    if not df_10y.empty:
        v = df_10y["value"].iloc[-1]
        prev = df_10y["value"].iloc[-2]
        c5.metric("Treasury 10Y", f"{v:.2f}%", f"{v - prev:+.2f}", delta_color="inverse")
    else:
        c5.metric("Treasury 10Y", "—")
    # ISM Prices Paid
    if ism_last is not None:
        delta_ism = f"{ism_last - ism_prev:+.1f}" if ism_prev else None
        c6.metric(f"ISM Prices ({ism_date})", f"{ism_last:.1f}", delta_ism, delta_color="inverse")
    else:
        c6.metric("ISM Prices Paid", "—")


def render_macro_table(fred_data: dict):
    # Definisce come formattare valore e delta per ciascuna serie
    SERIES_FORMAT = {
        # Tassi e spread (in %): delta in punti base
        "T10Y2Y":           ("rate_pp",   "% (punti curva)"),
        "T10Y3M":           ("rate_pp",   "% (punti curva)"),
        "DFF":              ("rate_pp",   "%"),
        "DGS10":            ("rate_pp",   "%"),
        "BAMLH0A0HYM2":     ("rate_pp",   "% (spread)"),
        # Indici di prezzo (livello): delta come variazione % MoM
        "CPIAUCSL":         ("index_pct", "indice"),
        "CPILFESL":         ("index_pct", "indice"),
        "PCEPILFE":         ("index_pct", "indice"),
        # Tasso disoccupazione: pp
        "UNRATE":           ("rate_pp",   "%"),
        # NFP: migliaia di posti
        "PAYEMS":           ("jobs_k",    "migliaia"),
        # ISM proxy: punti indice
        "PPCDFSA066MSFRBPHI": ("index_pt", "indice (0-100)"),
        "PPCDISA066MSFRBNY":  ("index_pt", "indice (0-100)"),
    }

    rows = []
    for code, label in FRED_INDICATORS.items():
        df = fred_data.get(code, pd.DataFrame())
        if df.empty or len(df) < 2:
            continue
        last  = df["value"].iloc[-1]
        prev  = df["value"].iloc[-2]
        date_curr = df["date"].iloc[-1]
        date_prev = df["date"].iloc[-2]
        delta = last - prev

        fmt_type, unit = SERIES_FORMAT.get(code, ("index_pt", ""))

        # Formatta valore
        if fmt_type == "jobs_k":
            val_str = f"{last:,.0f}K"
        elif fmt_type in ("rate_pp", "index_pt"):
            val_str = f"{last:.2f}"
        else:
            val_str = f"{last:.2f}"

        # Formatta delta con unità chiara
        if fmt_type == "index_pct":
            # Variazione % MoM sull'indice
            pct = (delta / prev * 100) if prev else 0
            delta_str = f"{pct:+.2f}% MoM"
        elif fmt_type == "rate_pp":
            bp = delta * 100
            delta_str = f"{bp:+.0f} bp"
        elif fmt_type == "jobs_k":
            delta_str = f"{delta:+,.0f}K"
        else:
            delta_str = f"{delta:+.2f} pt"

        ref_str = date_prev.strftime("%b %Y")
        rows.append({
            "Indicatore": label,
            "Valore": val_str,
            f"Δ vs {ref_str}": delta_str,
            "Unità": unit,
            "Data rilevazione": date_curr.strftime("%b %Y"),
        })

    if rows:
        df_out = pd.DataFrame(rows)

        def color_delta_str(v):
            if not isinstance(v, str) or v == "—":
                return ""
            try:
                num = float(v.split()[0].replace(",", ""))
                return "color: #2ecc71" if num > 0 else ("color: #e74c3c" if num < 0 else "")
            except Exception:
                return ""

        delta_col = [c for c in df_out.columns if c.startswith("Δ")][0]
        st.dataframe(
            df_out.style.map(color_delta_str, subset=[delta_col]),
            use_container_width=True,
            hide_index=True,
        )


def render_assets_table(prices_summary: dict):
    rows = []
    for ticker, label in ASSETS.items():
        d = prices_summary.get(ticker)
        if not d:
            continue

        def pct_str(v):
            if v is None or (isinstance(v, float) and pd.isna(v)):
                return "—"
            return f"{v:+.2f}%"

        rows.append({
            "Asset": label,
            "Close": f"{d['close']:.2f}",
            "1M": pct_str(d["change_1m"]),
            "3M": pct_str(d["change_3m"]),
            "YTD": pct_str(d["ytd"]),
            "Data": d["date"],
            # valori numerici nascosti per il coloring
            "_1m": d["change_1m"],
            "_3m": d["change_3m"],
            "_ytd": d["ytd"],
        })
    if rows:
        df_out = pd.DataFrame(rows)

        def color_pct_cell(v):
            if not isinstance(v, str) or v == "—":
                return ""
            try:
                num = float(v.replace("%", "").replace("+", ""))
                return "color: #2ecc71" if num > 0 else "color: #e74c3c"
            except Exception:
                return ""

        display_cols = ["Asset", "Close", "1M", "3M", "YTD", "Data"]
        st.dataframe(
            df_out[display_cols].style.map(color_pct_cell, subset=["1M", "3M", "YTD"]),
            use_container_width=True,
            hide_index=True,
        )


def render_sentiment_table(naaim_df: pd.DataFrame, putcall: dict, prices_summary: dict, prices_raw: pd.DataFrame):
    rows = []

    # NAAIM
    if not naaim_df.empty and len(naaim_df) >= 2:
        last = naaim_df["value"].iloc[-1]
        prev = naaim_df["value"].iloc[-2]
        delta = last - prev
        if last >= 90:
            reading = "🔴 Estrema esposizione long (contrarian bearish)"
        elif last >= 70:
            reading = "🟡 Alta esposizione"
        elif last <= 10:
            reading = "🟢 Capitulation (contrarian super-bullish)"
        elif last <= 30:
            reading = "🟢 Bassa esposizione (contrarian bullish)"
        else:
            reading = "⚪ Neutra"
        rows.append({"Indicatore": "NAAIM Exposure", "Valore": f"{last:.1f}%",
                     "Δ": f"{delta:+.1f}", "Lettura": reading})
    else:
        rows.append({"Indicatore": "NAAIM Exposure", "Valore": "—", "Δ": "—",
                     "Lettura": "Fetch fallito"})

    # Put/Call Total
    pc_total = putcall.get("total")
    if pc_total is not None:
        if pc_total > 1.20:
            r = "🟢 Estremo bearish (contrarian bullish)"
        elif pc_total > 1.00:
            r = "🟢 Bearish (precauzionale)"
        elif pc_total < 0.60:
            r = "🔴 Estremo bullish (contrarian bearish)"
        elif pc_total < 0.75:
            r = "🟡 Bullish (compiacimento)"
        else:
            r = "⚪ Neutro"
        rows.append({"Indicatore": "Put/Call Total (CBOE)", "Valore": f"{pc_total:.2f}",
                     "Δ": "today", "Lettura": r})

    # Put/Call Equity
    pc_equity = putcall.get("equity")
    if pc_equity is not None:
        if pc_equity > 0.80:
            r = "🟢 Equity bearish"
        elif pc_equity < 0.50:
            r = "🔴 Equity bullish (compiacimento)"
        else:
            r = "⚪ Neutro"
        rows.append({"Indicatore": "Put/Call Equity (CBOE)", "Valore": f"{pc_equity:.2f}",
                     "Δ": "today", "Lettura": r})

    # VIX
    vix = prices_summary.get("^VIX", {})
    if vix.get("close"):
        val = vix["close"]
        ch = vix.get("change_1m")
        if val > 40:
            r = "🟢 Panico estremo — capitulation (forte contrarian bullish)"
        elif val > 30:
            r = "🟢 Panico — fear elevato (contrarian bullish)"
        elif val > 20:
            r = "🟡 Stress — incertezza sopra la media"
        elif val > 14:
            r = "⚪ Normale — volatilità nella norma storica"
        elif val > 12:
            r = "🟡 Compiacimento — bassa protezione, attenzione"
        else:
            r = "🔴 Compiacimento estremo — mercato non prezza rischi"
        rows.append({"Indicatore": "VIX", "Valore": f"{val:.2f}",
                     "Δ": fmt_pct(ch), "Lettura": r})

    # Copper/Gold
    try:
        if (isinstance(prices_raw, pd.DataFrame)
                and "HG=F" in prices_raw.columns.get_level_values(0)
                and "GC=F" in prices_raw.columns.get_level_values(0)):
            copper = prices_raw["HG=F"]["Close"].dropna()
            gold = prices_raw["GC=F"]["Close"].dropna()
            if len(copper) > 0 and len(gold) > 0:
                ratio_now = copper.iloc[-1] / gold.iloc[-1]
                ch3m = None
                if len(copper) >= 66 and len(gold) >= 66:
                    ratio_3m = copper.iloc[-66] / gold.iloc[-66]
                    ch3m = (ratio_now / ratio_3m - 1) * 100
                if ch3m is not None and ch3m > 8:
                    r = "🟢 In rialzo: pro-growth, yields support"
                elif ch3m is not None and ch3m < -8:
                    r = "🔴 In calo: risk-off / growth slowing"
                else:
                    r = "⚪ Stabile"
                rows.append({"Indicatore": "Copper/Gold ×1000",
                             "Valore": f"{ratio_now * 1000:.2f}",
                             "Δ": fmt_pct(ch3m) if ch3m is not None else "—",
                             "Lettura": r})
    except Exception:
        pass

    # DXY
    dxy = prices_summary.get("DX-Y.NYB", {})
    if dxy.get("close"):
        ch = dxy.get("change_1m")
        if ch is not None and ch > 2:
            r = "🔴 In rafforzamento: headwind per oro/EM"
        elif ch is not None and ch < -2:
            r = "🟢 In indebolimento: tailwind per hard asset"
        else:
            r = "⚪ Lateralizzazione"
        rows.append({"Indicatore": "DXY", "Valore": f"{dxy['close']:.2f}",
                     "Δ": fmt_pct(ch), "Lettura": r})

    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


def render_sector_chart(sector_df: pd.DataFrame):
    if sector_df.empty:
        st.warning("Dati settoriali non disponibili")
        return

    df = sector_df.copy().dropna(subset=["rs_1m"])
    df = df.sort_values("rs_1m")

    fig = px.bar(
        df,
        x="rs_1m",
        y="label",
        orientation="h",
        color="rs_1m",
        color_continuous_scale=["#e74c3c", "#f39c12", "#2ecc71"],
        color_continuous_midpoint=0,
        labels={"rs_1m": "RS vs SPY 1M (%)", "label": ""},
        height=420,
    )
    fig.update_layout(
        margin=dict(l=0, r=0, t=10, b=10),
        coloraxis_showscale=False,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font_color="#ccc",
        xaxis=dict(gridcolor="#333", zeroline=True, zerolinecolor="#666"),
        yaxis=dict(gridcolor="rgba(0,0,0,0)"),
    )
    fig.add_vline(x=0, line_color="#555")
    st.plotly_chart(fig, use_container_width=True)


def render_sector_table(sector_df: pd.DataFrame):
    if sector_df.empty:
        return
    cols = ["label", "ticker", "close", "perf_1m", "perf_3m", "perf_ytd", "rs_1m", "pct_ma50", "pct_ma200"]
    df = sector_df[cols].copy()
    df.columns = ["Settore", "Ticker", "Close", "1M %", "3M %", "YTD %", "RS 1M %", "vs MA50 %", "vs MA200 %"]

    def color_num(v):
        if not isinstance(v, (int, float)) or pd.isna(v):
            return ""
        return "color: #2ecc71" if v > 0 else "color: #e74c3c"

    pct_cols = ["1M %", "3M %", "YTD %", "RS 1M %", "vs MA50 %", "vs MA200 %"]

    def _fmt_pct2(v):
        if not isinstance(v, (int, float)) or pd.isna(v):
            return "—"
        return f"{v:+.2f}%"

    def _fmt_close(v):
        if not isinstance(v, (int, float)) or pd.isna(v):
            return "—"
        return f"{v:,.2f}"

    fmt_dict = {c: _fmt_pct2 for c in pct_cols}
    fmt_dict["Close"] = _fmt_close

    st.dataframe(
        df.style
          .map(color_num, subset=pct_cols)
          .format(fmt_dict),
        use_container_width=True,
        hide_index=True,
    )


# =============================================================================
# ANALISI SETTORI LAGGARD
# =============================================================================

# Settori favoriti per regime macro (usati per incrociare RS con contesto)
SECTOR_REGIME_FIT = {
    "goldilocks":   ["Technology", "Discretionary", "Communications", "Financials", "Real Estate"],
    "reflation":    ["Energy", "Materials", "Industrials", "Financials", "US Defense", "Uranium"],
    "stagflation":  ["Energy", "Materials", "Gold miners", "Silver miners", "Uranium", "US Defense"],
    "late_cycle":   ["Healthcare", "Staples", "Utilities"],
    "disinflation": ["Healthcare", "Staples", "Utilities"],
    "transition":   [],
}


def render_sector_suggestions(sector_df: pd.DataFrame, phase: str):
    """
    Analisi cross-settoriale dei laggard: incrocia RS 1M/3M, distanza dalle medie
    mobili e regime macro per generare ipotesi di posizionamento con tesi chiara.
    """
    if sector_df.empty:
        return

    fit_sectors = SECTOR_REGIME_FIT.get(phase, [])
    phase_label = phase.upper().replace("_", " ")
    phase_color = PHASE_COLORS.get(phase, "#95a5a6")

    laggards = sector_df[sector_df["rs_1m"].notna() & (sector_df["rs_1m"] < 0)].sort_values("rs_1m")
    if laggards.empty:
        st.success("✅ Tutti i settori hanno RS positiva vs SPY — nessun laggard da analizzare.")
        return

    st.markdown(
        f"<div style='font-size:0.84rem; color:#999; margin-bottom:0.6rem;'>"
        f"Regime attivo: <span style='color:{phase_color}; font-weight:700;'>{phase_label}</span>"
        f" &nbsp;·&nbsp; Settori favoriti dal regime: "
        f"<span style='color:#aaa;'>{', '.join(fit_sectors) if fit_sectors else 'nessuno (transizione)'}</span>"
        f"</div>",
        unsafe_allow_html=True,
    )

    suggestions = []

    for _, row in laggards.iterrows():
        label       = row["label"]
        rs1m        = row.get("rs_1m")
        rs3m        = row.get("rs_3m")
        pct_ma50    = row.get("pct_ma50")
        pct_ma200   = row.get("pct_ma200")

        # Fit con regime: controlla corrispondenza parziale (es. "Gold miners" ∈ fit list)
        fits_regime = any(
            f.lower() in label.lower() or label.lower() in f.lower()
            for f in fit_sectors
        )

        # Stato tecnico
        above_ma200      = pct_ma200 is not None and pct_ma200 > 0
        above_ma50       = pct_ma50  is not None and pct_ma50  > 0
        near_ma200       = pct_ma200 is not None and -5 < pct_ma200 < 2
        deeply_below_ma200 = pct_ma200 is not None and pct_ma200 < -10
        # RS migliorante: pressione di vendita che si attenua
        rs_improving     = (rs3m is not None and rs1m is not None and rs1m > rs3m)

        ma200_tag = f"{pct_ma200:+.1f}% vs MA200" if pct_ma200 is not None else "MA200 n.d."
        ma50_tag  = f"{pct_ma50:+.1f}% vs MA50"  if pct_ma50  is not None else ""
        rs3m_tag  = f"RS 3M: {rs3m:+.1f}%"       if rs3m      is not None else "RS 3M n.d."

        if fits_regime:
            if above_ma200 and (rs_improving or above_ma50):
                signal = "🟢 OPPORTUNITÀ"
                color  = "#2ecc71"
                if rs_improving:
                    tesi = (
                        f"Laggard di breve in settore <b>allineato al regime {phase_label}</b>. "
                        f"La RS mensile ({rs1m:+.1f}%) è meno negativa della trimestrale ({rs3m:+.1f}%) "
                        f"→ la pressione di vendita si sta attenuando. Struttura tecnica integra "
                        f"({ma200_tag}). <b>Tesi: accumulo graduale su debolezza mensile</b>, stop tecnico "
                        f"sotto il supporto MA200."
                    )
                else:
                    tesi = (
                        f"Debolezza mensile in settore <b>favorito dal regime</b>, ma struttura tecnica intatta: "
                        f"{ma200_tag}, {ma50_tag}. La RS negativa potrebbe riflettere rotazione interna "
                        f"temporanea. <b>Tesi: costruire posizione in graduale</b>; il settore è sano, "
                        f"la RS dovrebbe riallinearsi se il regime tiene."
                    )
            elif near_ma200 or (above_ma200 and not above_ma50):
                signal = "🟡 WATCH"
                color  = "#f39c12"
                area   = f"vicino a MA200 ({ma200_tag})" if near_ma200 else f"sotto MA50 ma sopra MA200 ({ma200_tag})"
                tesi = (
                    f"Settore allineato al regime ma in zona tecnica borderline: {area}. "
                    f"RS 1M {rs1m:+.1f}%, {rs3m_tag}. "
                    f"<b>Attendere conferma del supporto</b> (chiusura settimanale sopra MA50) prima di "
                    f"costruire posizioni. Il regime è favorevole ma il timing è prematuro."
                )
            elif deeply_below_ma200:
                signal = "⚠️ REGIME OK — TECNICA KO"
                color  = "#e67e22"
                tesi = (
                    f"Settore teoricamente allineato al regime <b>ma con struttura tecnica compromessa</b>: "
                    f"{ma200_tag}. Una debolezza così profonda sotto MA200 segnala spesso deterioramento "
                    f"fondamentale, non solo momentum. <b>Non accumulare</b> contro un downtrend strutturale: "
                    f"attendere recupero e stabilizzazione sopra MA200."
                )
            else:
                signal = "🟡 WATCH"
                color  = "#f39c12"
                tesi = (
                    f"Regime favorevole ma debolezza tecnica ancora da risolvere ({ma200_tag}, {ma50_tag}). "
                    f"RS 1M {rs1m:+.1f}%. Monitorare per ingresso dopo segnale di forza relativa."
                )
        else:
            # Laggard per ragioni fondamentali (non allineato al regime)
            if rs1m is not None and rs1m < -12:
                signal = "🔴 EVITARE — nota estremo"
                color  = "#c0392b"
                tesi = (
                    f"Il regime <b>{phase_label} non favorisce {label}</b> — il ritardo ha radici "
                    f"fondamentali, non è rumore. RS 1M {rs1m:+.1f}%, {rs3m_tag}. "
                    f"<i>Nota:</i> la vendita estrema potrebbe generare un rimbalzo tecnico speculativo "
                    f"({ma200_tag}), ma senza cambio di regime non costruire posizioni strutturali. "
                    f"Eventuale trade solo con sizing piccolo e stop stretto."
                )
            else:
                signal = "🔴 EVITARE"
                color  = "#e74c3c"
                tesi = (
                    f"Il regime <b>{phase_label} non favorisce {label}</b>. "
                    f"RS 1M {rs1m:+.1f}%, {rs3m_tag}. {ma200_tag}. "
                    f"Non costruire posizioni contro-ciclo: aspettare segnale di cambio regime "
                    f"(CPI, ISM Prices, curva)."
                )

        suggestions.append({
            "signal": signal, "color": color, "label": label,
            "tesi": tesi, "fits_regime": fits_regime,
            "rs1m": rs1m, "rs3m": rs3m, "ma200": pct_ma200, "ma50": pct_ma50,
        })

    # Ordina: opportunità prima, poi watch, poi evita; a parità di segnale: rs1m desc
    _order = {"🟢 OPPORTUNITÀ": 0, "🟡 WATCH": 1,
               "⚠️ REGIME OK — TECNICA KO": 2, "🔴 EVITARE — nota estremo": 3, "🔴 EVITARE": 4}
    suggestions.sort(key=lambda x: (_order.get(x["signal"], 5), -(x["rs1m"] or 0)))

    for s in suggestions[:9]:       # max 9 settori
        rs3m_disp = f"RS 3M: {s['rs3m']:+.1f}%" if s["rs3m"] is not None else ""
        ma200_disp = f"{s['ma200']:+.1f}% vs MA200" if s["ma200"] is not None else ""
        meta = " · ".join(filter(None, [rs3m_disp, ma200_disp]))
        st.markdown(
            f"""
            <div style="border-left:4px solid {s['color']}; background:{s['color']}12;
                        border-radius:5px; padding:10px 14px; margin-bottom:9px;">
              <div style="display:flex; justify-content:space-between; align-items:baseline; flex-wrap:wrap; gap:4px;">
                <span style="font-weight:700; color:{s['color']}; font-size:0.88rem;">{s['signal']}</span>
                <span style="font-weight:600; font-size:0.9rem;">{s['label']}</span>
                <span style="color:#e74c3c; font-size:0.82rem;">RS 1M: {s['rs1m']:+.1f}%</span>
                <span style="color:#888; font-size:0.75rem;">{meta}</span>
              </div>
              <div style="font-size:0.78rem; color:#ccc; margin-top:6px; line-height:1.5;">{s['tesi']}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )


def render_cot_zscore_legend():
    """Spiega lo z-score COT in modo semplice."""
    with st.expander("📐 Cos'è lo Z-Score e come si legge?", expanded=False):
        st.markdown("""
#### Z-Score: quanto è insolita la posizione degli speculatori?

Immagina di tenere un registro di quante scommesse rialziste o ribassiste fanno gli hedge fund
su un mercato **ogni settimana per 5 anni** (260 settimane circa).

Lo **z-score** ti dice: *"rispetto a tutto lo storico, quanto è estrema la posizione di questa settimana?"*

| Z-Score | Cosa significa | In pratica |
|---|---|---|
| **0** | Esattamente nella media storica | Situazione normale, niente da segnalare |
| **+1** | Un po' sopra la media | Già in zona rialzista ma non estremo |
| **+2 o più** 🔴 | Molto sopra la media — accade <5% delle settimane | Gli speculatori sono **stracarichi di long**: chi compra ancora? |
| **−1** | Un po' sotto la media | Già in zona ribassista ma non estremo |
| **−2 o meno** 🟢 | Molto sotto la media — accade <5% delle settimane | Gli speculatori sono **stracarichi di short**: basta una buona notizia per il rimbalzo |

---

#### Perché funziona come segnale contrarian?

Gli speculatori (hedge fund, CTA) sono **trend follower**: aumentano le scommesse quando il trend è già in corso.
Quando arrivano a posizioni estreme, significa che **quasi tutti hanno già comprato (o venduto)** — non rimane
quasi nessuno da convertire. A quel punto basta un piccolo cambio di narrative per innescare una inversione.

> 🟢 **Z < −2** → specs capitolati: setup contrarian rialzista, potenziale rimbalzo
>
> 🔴 **Z > +2** → specs sovraffollati: mercato vulnerabile a correzione, non aggiungere long

---

#### Il Percentile (colonna "Percentile")
Dice la stessa cosa in modo diverso: **"95°"** significa che la posizione attuale è più alta del 95%
di tutte le settimane degli ultimi 5 anni. Se sei al 95° percentile long, sei in territorio estremo.
        """)


def render_cot_legend():
    """Pannello leggenda COT espandibile."""
    with st.expander("📖 Come leggere il COT Report", expanded=False):
        st.markdown("""
**Il Commitments of Traders (COT)** è un report settimanale pubblicato dalla CFTC ogni venerdì,
con dati aggiornati al martedì precedente. Mostra le posizioni aperte sui mercati futures suddivise
per categoria di operatore.

---

#### 🏭 Commercial (Produttori / Hedger)
> Aziende che usano i futures per coprire un'esposizione reale: miniere d'oro, compagnie petrolifere,
> trader di commodity, banche con esposizione valutaria.

- **Sono "smart money" strutturale**: sanno meglio di chiunque il valore fair del sottostante.
- **Tendenzialmente contrarian**: vendono futures quando il prezzo è alto (coprono produzione futura),
  comprano quando è basso (coprono acquisti futuri).
- **Commercial Net molto negativo** (short) → il mercato è caro secondo chi lo conosce meglio.
- **Commercial Net molto positivo** (long) → il mercato è a sconto, potenziale accumulo.

---

#### 📈 Managed Money / Speculatori (Large Specs)
> Hedge fund, CTA, fondi macro. Operano puramente per profitto, senza sottostante fisico.

- **Trend follower per definizione**: aumentano le posizioni quando il trend è in corso.
- **Contrarian ai massimi/minimi estremi**: posizione net long estrema = mercato sovraffollato,
  rischio inversione. Posizione net short estrema = potenziale short squeeze.
- **Specs Net molto long** → euforia, attenzione a reversal.
- **Specs Net molto short** → capitulation, potenziale rimbalzo.

---

#### 📊 Open Interest
Numero totale di contratti aperti (long + short sommati una sola volta).
- **OI in aumento + prezzo in salita** → trend confermato da nuovo denaro.
- **OI in calo + prezzo in salita** → rally su short covering, meno sostenibile.
- **OI in aumento + prezzo in calo** → pressione ribassista confermata.

---

#### 🧭 Come usare i dati in questa dashboard

| Segnale | Lettura |
|---|---|
| Commercial Net molto **long** + Specs Net molto **short** | Potenziale bottom, setup contrarian rialzista |
| Commercial Net molto **short** + Specs Net molto **long** | Mercato sovraffollato, rischio correzione |
| Specs Net > +15% su OI | 🟢 Bullish specs — trend in corso ma rischio affollamento |
| Specs Net < -15% su OI | 🔴 Bearish specs — capitulation o trend ribassista confermato |

---

#### ⚠️ Limiti da tenere a mente
- I dati sono con **3-4 giorni di ritardo** (pubblicati venerdì, dati al martedì).
- Il sentiment è qui **qualitativo** (% su open interest). Dopo 3-6 mesi di storico accumulato
  sarà possibile calcolare **z-score reali** per sapere se una posizione è estrema rispetto alla storia.
- I COT cambiano **una volta a settimana** — non ha senso aggiornarli più spesso.
        """)


def render_cot_table(cot_df: pd.DataFrame):
    """
    Tabella COT in due livelli:
    1. Segnale immediato — basato sulla settimana corrente (% specs su OI + direzione commercial)
    2. Contesto storico — z-score e percentile rank (in expander)
    """
    if cot_df.empty:
        st.warning("COT data non disponibili")
        return

    has_zscore = "mm_zscore" in cot_df.columns

    # ── LIVELLO 1: segnale della settimana corrente ──────────────────────────
    rows_now = []
    for _, row in cot_df.iterrows():
        mm_net   = row.get("mm_net")          or 0
        comm_net = row.get("commercial_net")  or 0
        oi       = row.get("open_interest")   or 0

        mm_pct_oi   = mm_net   / oi * 100 if oi > 0 else None
        comm_pct_oi = comm_net / oi * 100 if oi > 0 else None

        # Segnale speculatori (% su OI questa settimana)
        if mm_pct_oi is not None:
            if mm_pct_oi > 25:
                specs_sent = "🔴 Molto long"
            elif mm_pct_oi > 10:
                specs_sent = "🟡 Long"
            elif mm_pct_oi < -25:
                specs_sent = "🟢 Molto short"
            elif mm_pct_oi < -10:
                specs_sent = "🟢 Short"
            else:
                specs_sent = "⚪ Neutro"
        else:
            specs_sent = "—"

        # Segnale commercial (direzione contrarian)
        if comm_pct_oi is not None:
            if comm_pct_oi > 10:
                comm_sent = "🟢 Long (bullish)"
            elif comm_pct_oi < -10:
                comm_sent = "🔴 Short (bearish)"
            else:
                comm_sent = "⚪ Neutro"
        else:
            comm_sent = "—"

        # Setup combinato: la confluenza conta
        if mm_pct_oi is not None and comm_pct_oi is not None:
            if mm_pct_oi < -10 and comm_pct_oi > 10:
                setup = "🟢🟢 Contrarian rialzista"
            elif mm_pct_oi > 10 and comm_pct_oi < -10:
                setup = "🔴🔴 Contrarian ribassista"
            elif mm_pct_oi < -10:
                setup = "🟢 Specs short"
            elif mm_pct_oi > 10:
                setup = "🔴 Specs long"
            else:
                setup = "⚪ Neutro"
        else:
            setup = "—"

        rows_now.append({
            "Future":           row["label"],
            "Open Interest":    f"{int(oi):,}"        if oi      else "—",
            "Commercial Net":   f"{int(comm_net):+,}" if comm_net else "—",
            "Specs Net":        f"{int(mm_net):+,}"   if mm_net   else "—",
            "Specs %OI":        f"{mm_pct_oi:+.1f}%"  if mm_pct_oi  is not None else "—",
            "Specs segnale":    specs_sent,
            "Commercial":       comm_sent,
            "Setup":            setup,
        })

    df_now = pd.DataFrame(rows_now)

    def _color_setup(v):
        if not isinstance(v, str): return ""
        if "🟢🟢" in v: return "color:#2ecc71; font-weight:700"
        if "🔴🔴" in v: return "color:#e74c3c; font-weight:700"
        if "🟢" in v:   return "color:#27ae60"
        if "🔴" in v:   return "color:#c0392b"
        return ""

    def _color_pct(v):
        if not isinstance(v, str) or v == "—": return ""
        try:
            num = float(v.replace("%",""))
            if num > 20:  return "color:#e74c3c; font-weight:600"
            if num > 10:  return "color:#f39c12"
            if num < -20: return "color:#2ecc71; font-weight:600"
            if num < -10: return "color:#27ae60"
        except ValueError: pass
        return ""

    st.dataframe(
        df_now.style
              .map(_color_pct,   subset=["Specs %OI"])
              .map(_color_setup, subset=["Setup"]),
        use_container_width=True,
        hide_index=True,
    )
    st.caption("Specs %OI = posizione netta degli speculatori come % dell'open interest questa settimana. Setup verde = specs short + commercial long (contrarian rialzista).")

    # ── LIVELLO 2: contesto storico z-score (expander) ───────────────────────
    if has_zscore:
        with st.expander("📊 Contesto storico — Z-Score e percentile rank", expanded=False):
            rows_z = []
            for _, row in cot_df.iterrows():
                mm_z     = row.get("mm_zscore")
                comm_z   = row.get("commercial_zscore")
                pct_rank = row.get("mm_pct_rank")
                n_weeks  = int(row.get("n_weeks", 0))
                mm_net   = row.get("mm_net") or 0
                comm_net = row.get("commercial_net") or 0

                if mm_z is not None:
                    if mm_z > 2.0:    z_sent = "🔴 Estremo long"
                    elif mm_z > 1.0:  z_sent = "🟡 Long sopra media"
                    elif mm_z < -2.0: z_sent = "🟢 Estremo short"
                    elif mm_z < -1.0: z_sent = "🟢 Short sotto media"
                    else:             z_sent = "⚪ Nella norma"
                else:
                    z_sent = "—"

                rows_z.append({
                    "Future":     row["label"],
                    "Specs Net":  f"{int(mm_net):+,}"   if mm_net   else "—",
                    "Z Specs":    f"{mm_z:+.2f}"  if mm_z   is not None else "—",
                    "Z Comm":     f"{comm_z:+.2f}" if comm_z is not None else "—",
                    "Percentile": f"{pct_rank}°"   if pct_rank is not None else "—",
                    "Storico":    f"{n_weeks}w ({n_weeks//52}a)" if n_weeks else "—",
                    "Segnale Z":  z_sent,
                })

            df_z = pd.DataFrame(rows_z)

            def _color_z(v):
                if not isinstance(v, str) or v == "—": return ""
                try:
                    num = float(v)
                    if num > 2.0:  return "color:#e74c3c; font-weight:600"
                    if num > 1.0:  return "color:#f39c12"
                    if num < -2.0: return "color:#2ecc71; font-weight:600"
                    if num < -1.0: return "color:#27ae60"
                except ValueError: pass
                return ""

            st.dataframe(
                df_z.style.map(_color_z, subset=["Z Specs", "Z Comm"]),
                use_container_width=True,
                hide_index=True,
            )
            med_weeks = int(cot_df["n_weeks"].median()) if "n_weeks" in cot_df.columns else 0
            st.caption(
                f"Z-score calcolato su ~{med_weeks} settimane ({med_weeks // 52} anni di storico). "
                f"Z > +2 o < -2 = posizione nel 5% più estremo della storia. "
                f"Percentile = % delle settimane con posizione inferiore a quella corrente."
            )


def render_cot_extreme_signals(cot_df: pd.DataFrame):
    """
    Segnala posizioni COT con z-score |z| ≥ 1.5: genera tesi di posizionamento
    con analisi di confluenza tra speculatori e commercial.
    """
    if cot_df.empty or "mm_zscore" not in cot_df.columns:
        return

    extremes = cot_df[cot_df["mm_zscore"].abs() >= 1.5].copy()
    if extremes.empty:
        return

    st.subheader("🚨 Segnali COT estremi — posizioni fuori dalla norma storica")
    st.caption(
        "Mostrati solo i contratti con z-score speculatori |z| ≥ 1.5. "
        "Un segnale COT è più affidabile quando speculatori e commercial sono in direzioni opposte (confluenza)."
    )

    for _, row in extremes.sort_values("mm_zscore").iterrows():
        z        = row["mm_zscore"]
        comm_z   = row.get("commercial_zscore") or 0.0
        mm_net   = row["mm_net"]
        comm_net = row["commercial_net"]
        pct_rank = row.get("mm_pct_rank", 50)
        n_weeks  = int(row.get("n_weeks", 0))
        label    = row["label"]
        anni     = max(n_weeks // 52, 1)

        # Confluenza commercial
        if z < 0 and comm_z > 0.5:
            confluence = f"✅ <b>Confluenza alta:</b> i Commercial sono anch'essi in posizione long sopra la media storica (z={comm_z:+.2f}) → segnale contrarian di qualità elevata."
        elif z < 0 and comm_z > 0:
            confluence = f"🟡 <b>Confluenza parziale:</b> i Commercial leggermente long (z={comm_z:+.2f}) — segnale supportato ma non estremo."
        elif z < 0 and comm_z < -0.5:
            confluence = f"⚠️ <b>No confluenza:</b> i Commercial sono anch'essi short (z={comm_z:+.2f}) — il segnale contrarian è meno affidabile, possibile downtrend strutturale."
        elif z > 0 and comm_z < -0.5:
            confluence = f"✅ <b>Confluenza alta:</b> i Commercial sono short sotto la media (z={comm_z:+.2f}) → mercato sovraffollato con pressione hedger contrarian. Rischio inversione elevato."
        elif z > 0 and comm_z < 0:
            confluence = f"🟡 <b>Confluenza parziale:</b> i Commercial leggermente short (z={comm_z:+.2f}) — segnale di affollamento supportato ma non estremo."
        else:
            confluence = f"⚪ <b>No confluenza:</b> i Commercial non confermano (z={comm_z:+.2f}) — segnale meno affidabile."

        if z <= -2.0:
            color = "#2ecc71"
            title = f"🟢 SPECS ESTREMO SHORT — {label}"
            desc  = (
                f"Gli speculatori sono al {pct_rank}° percentile di posizionamento short negli ultimi {anni} anni "
                f"(z={z:+.2f}, {n_weeks} settimane di storico)."
            )
            tesi  = (
                f"<b>Setup contrarian rialzista:</b> specs capitolati. A questi livelli di short estremo "
                f"il mercato è spesso pronto per un rimbalzo su qualunque catalyst positivo (dato macro, "
                f"notizia geopolitica, short squeeze). "
                f"Tesi operativa: posizione long speculativa con stop tecnico sotto i minimi recenti, "
                f"size ridotto rispetto alla posizione strutturale."
            )
        elif z <= -1.5:
            color = "#27ae60"
            title = f"🟢 SPECS SHORT SIGNIFICATIVO — {label}"
            desc  = (
                f"Speculatori in posizione short rilevante ({pct_rank}° percentile, z={z:+.2f}, {anni} anni di storico)."
            )
            tesi  = (
                f"Posizionamento elevato ma non ancora ai livelli di capitulation estrema. "
                f"Monitorare per ulteriore deterioramento che potrebbe generare un segnale contrarian più pulito, "
                f"oppure attendere un primo segnale di forza prima di costruire long."
            )
        elif z >= 2.0:
            color = "#e74c3c"
            title = f"🔴 SPECS ESTREMO LONG — {label}"
            desc  = (
                f"Gli speculatori sono al {pct_rank}° percentile di posizionamento long negli ultimi {anni} anni "
                f"(z={z:+.2f}, {n_weeks} settimane di storico)."
            )
            tesi  = (
                f"<b>Mercato sovraffollato:</b> quasi tutti gli speculatori sono già inside. "
                f"Rischio di 'long squeeze' se arriva un catalyst negativo — chi deve vendere, vende tutti insieme. "
                f"Tesi operativa: <b>non aggiungere long</b> in questa fase; considerare hedging o riduzione. "
                f"Non è necessariamente un segnale di short immediato, ma il risk/reward è sfavorevole per nuovi ingressi."
            )
        else:  # z >= 1.5
            color = "#e67e22"
            title = f"🟡 SPECS LONG ELEVATO — {label}"
            desc  = (
                f"Speculatori in posizione long significativa ({pct_rank}° percentile, z={z:+.2f}, {anni} anni di storico)."
            )
            tesi  = (
                f"Posizionamento elevato — affollamento in costruzione. Non aggiungere long aggressivamente; "
                f"il risk/reward si sta deteriorando. Monitorare per segnali di distribuzione o di z-score che sale ulteriormente verso il territorio estremo."
            )

        st.markdown(
            f"""
            <div style="border-left:5px solid {color}; background:{color}14;
                        border-radius:6px; padding:13px 17px; margin-bottom:13px;">
              <div style="font-weight:700; color:{color}; font-size:1rem; margin-bottom:5px;">{title}</div>
              <div style="font-size:0.82rem; color:#ddd; margin-bottom:5px;">{desc}</div>
              <div style="font-size:0.78rem; color:#aaa; margin-bottom:7px;">{confluence}</div>
              <div style="font-size:0.79rem; color:#ccc; border-top:1px solid #2a2a2a; padding-top:7px;">
                📌 {tesi}
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )


POSITIONING_CLEAR = {
    "stagflation": {
        "intro": "I prezzi salgono forte ma l'economia rallenta. È il scenario peggiore per un portafoglio classico: le azioni scendono perché le aziende soffrono, e i bond scendono perché i tassi salgono. Bisogna proteggersi dall'inflazione e stare lontani dalla duration.",
        "buy": [
            ("🥇 Oro e Argento", "Quando i soldi perdono valore, la gente compra metalli preziosi. In stagflazione salgono quasi sempre perché sono riserve di valore reale, non legate a nessuna moneta."),
            ("⛽ Energia (XLE, petrolio, gas)", "Le aziende energetiche guadagnano DI PIÙ quando i prezzi delle materie prime salgono — sono loro stesse una delle cause dell'inflazione. Tieni il 'problema' nel portafoglio."),
            ("🛡️ Difesa europea (Rheinmetall, Leonardo…)", "Settore con ordini governativi pluriennali e fissi. Non dipende dai consumi dei cittadini, quindi regge anche quando l'economia rallenta."),
            ("📈 TIPS / BTPEi (bond anti-inflazione)", "Sono obbligazioni il cui valore cresce automaticamente con l'inflazione. Se il CPI sale del 5%, anche il tuo rendimento sale. Il nome lo dice: Treasury Inflation-Protected Securities."),
            ("💵 Cash e T-bill corti (< 1 anno)", "Quando i tassi sono alti, anche il cash parcheggiato a breve rende bene (3-5%). Tienilo disponibile: quando la situazione cambia, avrai liquidità per comprare a prezzi bassi."),
        ],
        "sell": [
            ("❌ Bond lunghi (> 10 anni)", "Matematica pura: se l'inflazione è alta, i tassi salgono, e i bond già emessi valgono meno. Un BTP 30y può perdere il 20-30% con un rialzo di tassi dell'1%."),
            ("❌ Tech e Growth (Nasdaq, titoli ad alto multiplo)", "Queste aziende valgono sui profitti lontani nel futuro. Quando i tassi salgono, quei profitti futuri 'pesano' di meno oggi. Multipli crollano."),
            ("❌ REIT (immobiliare quotato)", "Molto sensibile ai tassi: mutui più cari = meno domanda di immobili = meno utili per i REIT. Doppio danno in stagflazione."),
            ("❌ Consumer Discretionary (lusso, auto, viaggi)", "Quando i prezzi salgono, la gente smette di comprare il superfluo. Questi settori dipendono esattamente dalla spesa discrezionale che si contrae."),
        ],
    },
    "goldilocks": {
        "intro": "L'economia cresce bene e l'inflazione è sotto controllo. È il paradiso per chi investe in azioni: le aziende guadagnano, la Fed non stringe, i multipli si espandono. Momento di prendere rischio, non di stare in cash.",
        "buy": [
            ("📱 Tech e Growth (Nasdaq, titoli growth)", "Quando i tassi sono stabili o scendono, i profitti futuri 'pesano' di più. I titoli growth accelerano in questo regime — è il loro momento ideale."),
            ("🏬 Consumer Discretionary", "L'economia forte = la gente spende. Lusso, viaggi, ristoranti, auto: tutti beneficiano di un consumatore con lavoro e fiducia."),
            ("🏘️ REIT (immobiliare)", "Tassi stabili = mutui accessibili = domanda immobiliare forte. I REIT distribuiscono dividendi alti e apprezzano bene."),
            ("🌍 EM Equity (mercati emergenti)", "In goldilocks il dollaro è stabile e l'economia globale gira: i mercati emergenti beneficiano di un contesto internazionale favorevole."),
            ("💳 IG Credit (bond investment grade)", "Spread bassi, aziende sane: un carry positivo senza prendere rischi eccessivi."),
        ],
        "sell": [
            ("❌ Cash (troppo)", "Tenersi liquidi in goldilocks è un drag: le azioni rendono di più. Il cash erode il rendimento reale del portafoglio."),
            ("❌ Difensivi puri (Staples, Utilities)", "Settori che 'reggono le tempeste' ma non accelerano quando il sole splende. In goldilocks restano indietro: il mercato preferisce il rischio."),
            ("❌ Oro", "L'oro è un hedge per paura e inflazione. Se nessuno ha paura e l'inflazione è bassa, l'oro non ha ragione di salire."),
        ],
    },
    "reflation": {
        "intro": "L'economia sta accelerando e i prezzi salgono in modo moderato. È una ripresa ciclica: buone notizie per chi produce cose fisiche, cattive per chi ha bond lunghi. Il mercato ruota dal 'growth' al 'value'.",
        "buy": [
            ("🏭 Industriali e Materiali (XLI, XLB)", "Quando l'economia accelera, le fabbriche girano a pieno regime. Ordini in aumento, prezzi in rialzo: è il loro momento."),
            ("🏦 Finanziari (banche, assicurazioni)", "I tassi che salgono aumentano i margini delle banche. La crescita riduce i crediti in sofferenza. Doppio vantaggio."),
            ("⛽ Energia e Commodity", "La domanda globale in rialzo trascina i prezzi delle materie prime. Chi le produce guadagna di più."),
            ("📊 Small Cap Value", "Le piccole imprese cicliche sono le prime a beneficiare della ripresa, spesso con leverage operativo elevato."),
            ("📈 TIPS (parziale)", "Una copertura parziale sull'inflazione crescente ha senso, senza esagerare."),
        ],
        "sell": [
            ("❌ Bond lunghi (> 10 anni)", "I tassi salgono con la reflazione: i bond lunghi perdono prezzo. Più lunga la scadenza, più si perde."),
            ("❌ Mega cap Growth (Apple, Nvidia…)", "Ottimi business, ma in reflazione la rotazione favorisce i ciclici. Restano indietro non perché vadano male, ma perché ci sono cose che vanno meglio."),
            ("❌ Staples (beni di prima necessità)", "Difensivi a bassa crescita. In una fase espansiva il mercato preferisce prendere rischio ciclico."),
        ],
    },
    "late_cycle": {
        "intro": "La curva dei rendimenti è invertita: i tassi a breve sono più alti di quelli a lungo. Storicamente questo ha preceduto ogni recessione degli ultimi 50 anni, con un anticipo medio di 12-18 mesi. L'economia regge ancora, ma bisogna iniziare a ridurre il rischio gradualmente.",
        "buy": [
            ("🏥 Healthcare e Staples (XLV, XLP)", "Le persone si ammalano e mangiano anche in recessione. Questi settori hanno flussi di cassa stabili e difensivi indipendentemente dal ciclo."),
            ("🥇 Oro", "Quando arriva la paura di recessione, la gente compra oro come rifugio. I tassi reali tendono a scendere, e l'oro reagisce bene."),
            ("💵 Cash e T-bill corti (< 1 anno)", "Con la curva invertita, i T-bill a 3-6 mesi rendono QUANTO O PIÙ dei bond lunghi, senza il rischio duration. Parcheggia lì e aspetta."),
            ("🔒 IG Credit corto", "Tieniti su bond di aziende solide ma con scadenze brevi: carry positivo senza scommettere sul lungo termine."),
        ],
        "sell": [
            ("❌ High Yield (junk bond)", "In avvicinamento alla recessione, le aziende deboli iniziano a soffrire. Gli spread si allargano e il prezzo crolla. Esci prima."),
            ("❌ Ciclici (Industriali, Materiali)", "I loro utili dipendono dalla crescita economica. Se la recessione arriva, sono i primi a deludere le aspettative."),
            ("❌ Banche", "La curva invertita comprime il margine di interesse delle banche (guadagnano meno tra tassi brevi e lunghi). Doppio rischio: meno utili ora + più crediti in sofferenza in recessione."),
            ("❌ High Beta e speculativo", "Riduci tutto ciò che amplifica i movimenti del mercato. In late cycle non è il momento di scommesse aggressive."),
        ],
    },
    "disinflation": {
        "intro": "L'economia sta contraendosi e i prezzi scendono. La Fed ha tagliato o sta tagliando i tassi aggressivamente. I bond lunghi salgono (quando i tassi scendono, i bond già emessi valgono di più). È il momento dei difensivi e della duration lunga.",
        "buy": [
            ("📉 Treasury lunghi (> 10 anni, TLT)", "Quando i tassi scendono, i bond già emessi a tassi più alti valgono di più. Un Treasury 30y può guadagnare il 20-30% con un calo dei tassi dell'1%. È il trade principale in recessione."),
            ("🥇 Oro", "I tassi reali calanti sono il miglior driver per l'oro. Meno rendimento dai bond sicuri = più appeal per l'oro come riserva di valore."),
            ("🏥 Healthcare e Utilities", "Dividendi stabili, domanda anelastica. In recessione la gente taglia le vacanze, non le medicine."),
            ("💳 IG Credit lungo", "Le aziende solide tengono, gli spread si comprimono con i tagli Fed. Carry + apprezzamento del capitale."),
        ],
        "sell": [
            ("❌ Ciclici (Industriali, Energia, Materiali)", "La domanda globale crolla in recessione. I prezzi delle commodity scendono, gli utili crollano."),
            ("❌ Banche", "Tassi bassi comprimono i margini. Aumento dei crediti in sofferenza. Classico settore da evitare in recessione."),
            ("❌ High Yield", "In recessione salgono i default delle aziende deboli. Gli spread si allargano violentemente. Esci prima che diventi illiquido."),
            ("❌ EM Equity", "Risk aversion globale = flight to quality verso dollaro e Treasury USA. I mercati emergenti soffrono doppiamente."),
        ],
    },
    "transition": {
        "intro": "I dati macro mandano segnali contrastanti: nessuna regola domina chiaramente. Può essere un turning point tra un regime e l'altro, o l'effetto di uno shock esogeno. In questa fase la cosa più intelligente è NON fare mosse aggressive e aspettare che il quadro si chiarisca.",
        "buy": [
            ("💵 Cash e T-bill (opzionalità)", "Non sai ancora dove andare. Il cash ti dà la flessibilità di entrare velocemente quando il segnale arriva. Non è 'non fare niente': è preservare l'opzione di agire."),
            ("🥇 Oro (barbell)", "Un po' di oro come hedge funziona in quasi tutti i regimi. In transizione è una copertura a basso costo contro i possibili scenari negativi."),
            ("🔒 Posizioni esistenti invariate", "Non uscire di scatto da ciò che già hai senza una thesis chiara. Tieni, non aggiungere."),
        ],
        "sell": [
            ("❌ Leverage e concentrazione", "Il rischio peggiore in fase di transizione è avere posizioni molto concentrate o con leva finanziaria. Se sbaglia il regime, perdi il doppio."),
            ("❌ Posizioni direzionali forti nuove", "Aspetta il segnale chiaro. CPI prossimo, ISM, curva: uno di questi dirà dove stiamo andando. Entra DOPO."),
        ],
    },
}


def render_positioning(phase: str):
    """Raccomandazione di posizionamento chiara e diretta per il regime corrente."""
    data = POSITIONING_CLEAR.get(phase, POSITIONING_CLEAR["transition"])
    phase_color = PHASE_COLORS.get(phase, "#95a5a6")

    st.markdown(
        f"<div style='font-size:0.85rem; color:#ccc; line-height:1.6; margin-bottom:0.8rem;'>"
        f"{data['intro']}"
        f"</div>",
        unsafe_allow_html=True,
    )

    st.markdown(
        f"<div style='font-size:0.78rem; font-weight:700; color:{phase_color}; "
        f"text-transform:uppercase; letter-spacing:0.05em; margin-bottom:4px;'>"
        f"✅ Cosa comprare / tenere</div>",
        unsafe_allow_html=True,
    )
    for title, reason in data["buy"]:
        st.markdown(
            f"<div style='font-size:0.78rem; color:#ddd; margin-bottom:5px; padding-left:6px;'>"
            f"<b>{title}</b> — <span style='color:#aaa;'>{reason}</span></div>",
            unsafe_allow_html=True,
        )

    st.markdown(
        "<div style='font-size:0.78rem; font-weight:700; color:#e74c3c; "
        "text-transform:uppercase; letter-spacing:0.05em; margin-top:10px; margin-bottom:4px;'>"
        "❌ Cosa ridurre / evitare</div>",
        unsafe_allow_html=True,
    )
    for title, reason in data["sell"]:
        st.markdown(
            f"<div style='font-size:0.78rem; color:#ddd; margin-bottom:5px; padding-left:6px;'>"
            f"<b>{title}</b> — <span style='color:#aaa;'>{reason}</span></div>",
            unsafe_allow_html=True,
        )


# =============================================================================
# LEGENDA VIX
# =============================================================================

def render_vix_legend():
    with st.expander("📖 Legenda VIX — soglie e interpretazione", expanded=False):
        st.markdown("""
Il **VIX (CBOE Volatility Index)** misura la volatilità implicita attesa a 30 giorni sulle opzioni S&P 500.
È il termometro della paura del mercato — si legge in chiave **contrarian**.

| Livello VIX | Zona | Interpretazione contrarian |
|---|---|---|
| **> 40** | 🟢 Panico estremo | Capitulation: storicamente i migliori punti di entrata su equity |
| **30 – 40** | 🟢 Panico | Vendite forzate, spread alti: opportunità per compratori pazienti |
| **20 – 30** | 🟡 Stress elevato | Incertezza sopra la media, protezione cara ma necessaria |
| **14 – 20** | ⚪ Normale | Range storico neutro, nessun segnale direzionale |
| **12 – 14** | 🟡 Compiacimento | Poca protezione comprata: mercato vulnerabile a shock |
| **< 12** | 🔴 Compiacimento estremo | Euforia: storicamente associato a top di mercato o correzioni imminenti |

**Nota:** il VIX è un indicatore di breve termine. I segnali estremi (< 12 o > 40) hanno valore contrarian
significativo; i range intermedi vanno letti in contesto con NAAIM e Put/Call.
        """)


# =============================================================================
# GRAFICO YIELD CURVE
# =============================================================================

def render_yield_curve_chart(fred_data: dict):
    """Grafico storico curva 10Y-2Y e 10Y-3M con zona di inversione colorata."""
    df_10y2y = fred_data.get("T10Y2Y", pd.DataFrame())
    df_10y3m = fred_data.get("T10Y3M", pd.DataFrame())

    if df_10y2y.empty and df_10y3m.empty:
        st.warning("Dati curva non disponibili")
        return

    fig = go.Figure()

    for df, name, color in [
        (df_10y2y, "10Y – 2Y", "#3498db"),
        (df_10y3m, "10Y – 3M", "#e67e22"),
    ]:
        if df.empty:
            continue
        df_plot = df.tail(756)  # ~3 anni di dati giornalieri
        fig.add_trace(go.Scatter(
            x=df_plot["date"], y=df_plot["value"],
            name=name, line=dict(color=color, width=2),
            hovertemplate=f"{name}: %{{y:.2f}}%<br>%{{x|%d %b %Y}}<extra></extra>",
        ))
        # Area rossa sotto lo zero (inversione)
        fig.add_trace(go.Scatter(
            x=df_plot["date"], y=df_plot["value"].clip(upper=0),
            fill="tozeroy", fillcolor="rgba(231,76,60,0.15)",
            line=dict(width=0), showlegend=False, hoverinfo="skip",
        ))
        # Area verde sopra lo zero (normale)
        fig.add_trace(go.Scatter(
            x=df_plot["date"], y=df_plot["value"].clip(lower=0),
            fill="tozeroy", fillcolor="rgba(46,204,113,0.08)",
            line=dict(width=0), showlegend=False, hoverinfo="skip",
        ))

    fig.add_hline(y=0, line_color="#e74c3c", line_dash="dash", line_width=1.5,
                  annotation_text="Soglia inversione", annotation_position="bottom right",
                  annotation_font_color="#e74c3c")

    fig.update_layout(
        height=300,
        margin=dict(l=0, r=0, t=10, b=10),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font_color="#ccc",
        legend=dict(orientation="h", y=1.08, x=0),
        xaxis=dict(gridcolor="#2a2a2a", showgrid=True),
        yaxis=dict(gridcolor="#2a2a2a", showgrid=True, ticksuffix="%",
                   zeroline=True, zerolinecolor="#e74c3c", zerolinewidth=1),
        hovermode="x unified",
    )
    st.plotly_chart(fig, use_container_width=True)

    with st.expander("📖 Come leggere la curva dei rendimenti", expanded=False):
        st.markdown("""
La **curva dei rendimenti** misura lo spread tra il Treasury a lungo termine e quello a breve.

| Condizione | Zona | Cosa significa |
|---|---|---|
| **Spread > 0** (curva normale) | 🟢 | Gli investitori richiedono più rendimento per il lungo termine → aspettative di crescita |
| **Spread = 0** (curva piatta) | 🟡 | Segnale di rallentamento economico in arrivo |
| **Spread < 0** (curva invertita) | 🔴 | Recessione anticipata: storicamente ha preceduto 8 delle ultime 8 recessioni USA |

**Lead time storico:** l'inversione anticipa la recessione in media di **12-18 mesi**.
La **disinversione** (da negativo a positivo) spesso coincide con l'inizio effettivo della recessione — è lì che la disoccupazione inizia a salire.

- **10Y – 2Y**: il più monitorato dagli operatori. Sensibile alle aspettative Fed.
- **10Y – 3M**: preferito dalla ricerca Fed di NY come predittore recessione.
        """)


# =============================================================================
# LEGGENDA QUADRANTI CICLO MACRO
# =============================================================================

CYCLE_QUADRANTS = [
    {
        "phase": "goldilocks",
        "emoji": "🟢",
        "label": "GOLDILOCKS",
        "trigger": "CPI < 2,5% · Disoccupazione in calo · Curva positiva",
        "desc": "Il regime ideale: crescita solida, inflazione sotto controllo, mercato del lavoro forte. La Fed è ferma o taglia. Multipli in espansione, risk-on diffuso.",
        "asset_ok": "Equity growth (Tech, Discretionary) · Small/mid cap · Credit IG · REIT · EM equity",
        "asset_ko": "Cash (drag) · Difensivi puri (Staples, Utilities) · Oro · Long duration eccessiva",
        "color": "#2ecc71",
    },
    {
        "phase": "reflation",
        "emoji": "🔵",
        "label": "REFLATION",
        "trigger": "CPI 2–4% · Curva positiva · Crescita in accelerazione",
        "desc": "Ripresa ciclica: domanda in rialzo trascina sia crescita che inflazione moderata. I tassi salgono ma l'economia regge. Value batte Growth. Ciclici e commodity guidano.",
        "asset_ok": "Ciclici (Industrials, Financials, Materials) · Energy · Commodity · Small cap value · TIPS",
        "asset_ko": "Long duration (> 10y) · Staples · Mega cap growth · Cash",
        "color": "#3498db",
    },
    {
        "phase": "stagflation",
        "emoji": "🔴",
        "label": "STAGFLATION",
        "trigger": "CPI > 3% · ISM Prices > 70 · Crescita in decelerazione",
        "desc": "Il regime peggiore per portafogli 60/40: inflazione persistente e crescita che rallenta. La Fed stringe anche in frenata. Real asset e inflation hedge battono tutto.",
        "asset_ok": "Oro · Argento · Energia · Difesa EU · TIPS/BTPEi · Cash corto · Value vs Growth",
        "asset_ko": "Bond lunghi · Discretionary · REIT · HY credit · Growth tech · EM bonds",
        "color": "#e74c3c",
    },
    {
        "phase": "late_cycle",
        "emoji": "🟡",
        "label": "LATE CYCLE",
        "trigger": "Curva 10Y-2Y < 0 (invertita) · Segnali di decelerazione",
        "desc": "La curva invertita anticipa recessione con 12-18 mesi di lead time medio storico. Il mercato può ancora salire ma la qualità diventa fondamentale. Ridurre beta.",
        "asset_ok": "Quality defensives (Staples, Healthcare) · Oro · T-bill/cash < 1y · Min volatility · IG corto",
        "asset_ko": "High beta · High Yield · Ciclici · Financials · EM equity",
        "color": "#f39c12",
    },
    {
        "phase": "disinflation",
        "emoji": "🟣",
        "label": "DISINFLATION / RECESSIONE",
        "trigger": "CPI < 2% · Disoccupazione in rialzo > +0,3pp in 6m",
        "desc": "Domanda in contrazione, prezzi in discesa, lavoro in deterioramento. La Fed taglia aggressivamente. Bond lunghi e oro outperformano. Difensivi reggono.",
        "asset_ok": "Long duration treasury (> 10y) · Quality defensives · Oro · IG lungo · Dividend growers",
        "asset_ko": "Ciclici · Commodity · Banche · High Yield · EM equity",
        "color": "#9b59b6",
    },
    {
        "phase": "transition",
        "emoji": "⚪",
        "label": "TRANSITION",
        "trigger": "Segnali contrastanti — nessuna regola prevalente",
        "desc": "Dati macro non convergenti: tipico nei turning point o dopo shock esogeni. Massima opzionalità, nessun rischio direzionale concentrato.",
        "asset_ok": "Cash/T-bill per opzionalità · Posizioni esistenti invariate · Barbell quality + oro",
        "asset_ko": "Leverage · Concentrazione · Posizioni illiquide senza thesis chiara",
        "color": "#95a5a6",
    },
]


def render_cycle_legend():
    """Griglia 3×2 con i sei quadranti del ciclo macro."""
    with st.expander("🗺️ Legenda quadranti del ciclo macro", expanded=False):
        cols = st.columns(3)
        for i, q in enumerate(CYCLE_QUADRANTS):
            with cols[i % 3]:
                st.markdown(
                    f"""
                    <div style="
                        border-left: 5px solid {q['color']};
                        background: {q['color']}14;
                        border-radius: 6px;
                        padding: 12px 14px;
                        margin-bottom: 14px;
                        min-height: 210px;
                    ">
                        <div style="font-size:1rem; font-weight:700; color:{q['color']}; margin-bottom:4px;">
                            {q['emoji']} {q['label']}
                        </div>
                        <div style="font-size:0.72rem; color:#aaa; margin-bottom:6px;">
                            <b>Trigger:</b> {q['trigger']}
                        </div>
                        <div style="font-size:0.78rem; color:#ddd; margin-bottom:8px;">
                            {q['desc']}
                        </div>
                        <div style="font-size:0.72rem; color:#2ecc71;">
                            ▲ {q['asset_ok']}
                        </div>
                        <div style="font-size:0.72rem; color:#e74c3c; margin-top:3px;">
                            ▼ {q['asset_ko']}
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )


def render_cycle_reasoning(phase: str, cpi_yoy, curve_last, unrate_last, unrate_trend, ism_last=None, ism_prev=None, ism_date="—"):
    """Spiega con i dati reali perché è stato identificato il ciclo corrente."""
    color = PHASE_COLORS.get(phase, "#95a5a6")
    q = next((x for x in CYCLE_QUADRANTS if x["phase"] == phase), None)

    def val_or_dash(v, fmt=".2f"):
        return f"{v:{fmt}}" if v is not None else "n.d."

    unrate_trend_str = (
        f"{unrate_trend:+.2f}pp in 6 mesi" if unrate_trend is not None else "n.d."
    )
    curve_str = val_or_dash(curve_last)
    cpi_str = val_or_dash(cpi_yoy, ".1f") + "%" if cpi_yoy is not None else "n.d."

    # Costruisci le check di ogni condizione
    checks = []
    if cpi_yoy is not None:
        if cpi_yoy < 2.5:
            checks.append(f"✅ CPI YoY = **{cpi_str}** → sotto 2,5% (non inflazionistico)")
        elif cpi_yoy <= 4:
            checks.append(f"🟡 CPI YoY = **{cpi_str}** → range 2–4% (reflazionistico)")
        else:
            checks.append(f"❌ CPI YoY = **{cpi_str}** → sopra 4% (inflazione elevata)")
    if ism_last is not None:
        delta_str = f" (Δ {ism_last - ism_prev:+.1f} vs mese prec.)" if ism_prev else ""
        if ism_last > 70:
            checks.append(f"❌ ISM Prices Paid = **{ism_last:.1f}** ({ism_date}){delta_str} → > 70 (pressioni inflative forti)")
        elif ism_last > 60:
            checks.append(f"🟡 ISM Prices Paid = **{ism_last:.1f}** ({ism_date}){delta_str} → > 60 (pressioni moderate)")
        elif ism_last >= 50:
            checks.append(f"🟡 ISM Prices Paid = **{ism_last:.1f}** ({ism_date}){delta_str} → in espansione ma contenuto")
        else:
            checks.append(f"✅ ISM Prices Paid = **{ism_last:.1f}** ({ism_date}){delta_str} → < 50 (prezzi in contrazione)")
        checks.append("ℹ️ Fonte: Trading Economics · scala 0-100 · >50 = prezzi in espansione · >70 = pressioni forti")
    else:
        checks.append("⚠️ ISM Prices Paid = **n.d.** (fetch Trading Economics fallito)")
    if curve_last is not None:
        if curve_last >= 0:
            checks.append(f"✅ Curva 10Y-2Y = **{curve_str}%** → positiva (non invertita)")
        else:
            checks.append(f"❌ Curva 10Y-2Y = **{curve_str}%** → invertita (segnale recessivo)")
    if unrate_trend is not None:
        if unrate_trend < 0:
            checks.append(f"✅ Disoccupazione in **calo** ({unrate_trend_str}) → mercato del lavoro forte")
        elif unrate_trend > 0.3:
            checks.append(f"❌ Disoccupazione in **rialzo** ({unrate_trend_str}) → indebolimento lavoro")
        else:
            checks.append(f"🟡 Disoccupazione stabile ({unrate_trend_str})")
    if unrate_last is not None:
        checks.append(f"ℹ️ Tasso disoccupazione attuale: **{unrate_last:.1f}%**")

    checks_md = "\n".join(f"- {c}" for c in checks)
    trigger = q["trigger"] if q else "—"

    with st.expander(f"🔍 Perché **{phase.upper().replace('_', ' ')}**? — ragionamento del modello", expanded=False):
        st.markdown(
            f"La fase è determinata dalla **prima regola soddisfatta** nel modello a priorità. "
            f"Regola attivata: `{trigger}`\n\n"
            f"**Valori osservati oggi:**\n{checks_md}"
        )


# =============================================================================
# APP PRINCIPALE
# =============================================================================

def main():
    # --- Top bar: logo + titolo + info + bottone refresh ---
    col_title, col_info, col_btn = st.columns([5, 3, 2])
    with col_title:
        last_update = st.session_state.get("last_update", "—")
        st.markdown(
            f"""
            <div style="display:flex; align-items:center; gap:14px; padding-bottom:4px;">
              {DODO_LOGO_SVG}
              <div>
                <div style="font-size:1.9rem; font-weight:800; letter-spacing:-0.03em;
                            color:#fff; line-height:1.05;">AskDodo</div>
                <div style="font-size:0.75rem; color:#666; margin-top:1px; letter-spacing:0.04em;">
                  MACRO INTELLIGENCE DASHBOARD
                </div>
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with col_info:
        last_update = st.session_state.get("last_update", "—")
        st.markdown(
            f"<div style='padding-top:14px; color:#666; font-size:0.82rem;'>"
            f"Aggiornato: {last_update}<br>"
            f"<span style='color:#444;'>FRED · yfinance · NAAIM · CBOE · CFTC</span>"
            f"</div>",
            unsafe_allow_html=True,
        )
    with col_btn:
        st.markdown("<div style='padding-top:10px;'></div>", unsafe_allow_html=True)
        if st.button("🔄 Aggiorna dati", type="primary", use_container_width=True):
            st.session_state["refresh_token"] = st.session_state.get("refresh_token", 0) + 1
            st.session_state["cot_loaded"] = False
            load_main_data.clear()
            load_cot_data.clear()

    # --- Carico dati principali ---
    refresh_token = st.session_state.get("refresh_token", 0)
    with st.spinner("Carico dati… (~10-15s)"):
        (fred_data, prices_summary, prices_raw,
         naaim_df, putcall, sector_df, ism_df, errors) = load_main_data(refresh_token)

    st.session_state["last_update"] = datetime.now().strftime("%d/%m/%Y %H:%M")

    if errors:
        with st.expander(f"⚠️ {len(errors)} avvisi fetch", expanded=False):
            for e in errors:
                st.caption(e)

    st.divider()

    # --- Ciclo macro ---
    cpi_yoy = compute_yoy(fred_data.get("CPIAUCSL", pd.DataFrame()))
    unrate_df = fred_data.get("UNRATE", pd.DataFrame())
    unrate_last = unrate_df["value"].iloc[-1] if not unrate_df.empty else None
    unrate_6m_ago = unrate_df["value"].iloc[-7] if len(unrate_df) > 7 else None
    unrate_trend = (unrate_last - unrate_6m_ago) if (unrate_last and unrate_6m_ago) else None
    curve_df = fred_data.get("T10Y2Y", pd.DataFrame())
    curve_last = curve_df["value"].iloc[-1] if not curve_df.empty else None

    # ISM Manufacturing Prices Paid (dato reale da Trading Economics)
    ism_last = float(ism_df["value"].iloc[-1]) if not ism_df.empty else None
    ism_prev = float(ism_df["value"].iloc[-2]) if len(ism_df) >= 2 else None
    ism_date = ism_df["date"].iloc[-1].strftime("%b %Y") if not ism_df.empty else "—"

    phase, phase_desc = classify_cycle_phase({
        "cpi_yoy": cpi_yoy, "unrate": unrate_last,
        "unrate_trend": unrate_trend, "curve_10y2y": curve_last,
        "ism_prices": ism_last,
    })

    render_phase_banner(phase, phase_desc)
    render_cycle_reasoning(phase, cpi_yoy, curve_last, unrate_last, unrate_trend, ism_last, ism_prev, ism_date)
    render_cycle_legend()

    st.divider()

    # --- KPI cards ---
    render_kpi_row(fred_data, prices_summary, ism_last, ism_prev, ism_date)

    st.divider()

    # --- Macro + Asset ---
    col_macro, col_asset = st.columns(2)
    with col_macro:
        st.subheader("📋 Indicatori macro (FRED)")
        render_macro_table(fred_data)
    with col_asset:
        st.subheader("💹 Asset & prezzi")
        render_assets_table(prices_summary)

    # --- Yield Curve Chart ---
    st.subheader("📉 Curva dei rendimenti (10Y-2Y · 10Y-3M)")
    render_yield_curve_chart(fred_data)

    st.divider()

    # --- Sentiment + Posizionamento ---
    col_sent, col_pos = st.columns([3, 2])
    with col_sent:
        st.subheader("🌡️ Sentiment & regime")
        render_sentiment_table(naaim_df, putcall, prices_summary, prices_raw)
        render_vix_legend()
    with col_pos:
        st.subheader("🎯 Posizionamento suggerito")
        render_positioning(phase)

    st.divider()

    # --- Rotazione settoriale ---
    st.subheader("📈 Rotazione settoriale — RS vs SPY 1M")
    col_chart, col_tbl = st.columns([2, 3])
    with col_chart:
        render_sector_chart(sector_df)
    with col_tbl:
        render_sector_table(sector_df)

    st.subheader("🎯 Ipotesi di posizionamento sui settori laggard")
    render_sector_suggestions(sector_df, phase)

    st.divider()

    # --- COT (on-demand) ---
    st.subheader("💼 Posizionamento COT")
    render_cot_legend()

    cot_token = st.session_state.get("cot_token", 0)
    cot_loaded = st.session_state.get("cot_loaded", False)

    if not cot_loaded:
        col_cot, _ = st.columns([2, 5])
        with col_cot:
            if st.button("📥 Carica dati COT (30-60s)", use_container_width=True):
                st.session_state["cot_token"] = cot_token + 1
                st.session_state["cot_loaded"] = True
                st.rerun()
        st.caption("Il COT (CFTC) scarica ~30MB di dati. Clicca solo quando vuoi analizzare il posizionamento.")
    else:
        with st.spinner("Scarico 5 anni di dati CFTC e calcolo z-score… (prima volta: ~2 min, poi cached)"):
            cot_df, cot_err = load_cot_data(st.session_state["cot_token"])
        if cot_err:
            st.warning(f"COT fetch fallito: {cot_err}")
        else:
            render_cot_table(cot_df)
            render_cot_zscore_legend()
            render_cot_extreme_signals(cot_df)
        col_reset, _ = st.columns([2, 5])
        with col_reset:
            if st.button("🔄 Ricarica COT", use_container_width=True):
                load_cot_data.clear()
                st.session_state["cot_token"] = st.session_state.get("cot_token", 0) + 1
                st.rerun()


if __name__ == "__main__":
    main()
