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
    fetch_cftc_data, compute_yoy, compute_sector_metrics, extract_cot_positioning,
    classify_cycle_phase, get_positioning_recommendation,
    FRED_INDICATORS, ASSETS, SECTORS, COT_CONTRACTS,
)

# =============================================================================
# CONFIG PAGINA
# =============================================================================

st.set_page_config(
    page_title="Macro Dashboard",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed",
)

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
def load_all_data(refresh_token: int):
    """Scarica tutti i dati incluso COT. refresh_token cambia a ogni click su Aggiorna."""
    fred_data, prices_summary, prices_raw = {}, {}, pd.DataFrame()
    naaim_df, cot_df, putcall = pd.DataFrame(), pd.DataFrame(), {"total": None, "equity": None}
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

    # Sector metrics
    sector_df = pd.DataFrame()
    try:
        sector_df = compute_sector_metrics(prices_raw, spy_close)
    except Exception as e:
        errors.append(f"Sectors: {e}")

    # COT (sempre attivo)
    try:
        cot_raw = fetch_cftc_data()
        cot_df = extract_cot_positioning(cot_raw)
    except Exception as e:
        errors.append(f"COT: {e}")

    return fred_data, prices_summary, prices_raw, naaim_df, putcall, sector_df, cot_df, errors


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


def render_kpi_row(fred_data: dict, prices_summary: dict):
    c1, c2, c3, c4, c5 = st.columns(5)
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


def render_macro_table(fred_data: dict):
    rows = []
    for code, label in FRED_INDICATORS.items():
        df = fred_data.get(code, pd.DataFrame())
        if df.empty or len(df) < 2:
            continue
        last = df["value"].iloc[-1]
        prev = df["value"].iloc[-2]
        delta = last - prev
        date = df["date"].iloc[-1].strftime("%d-%m-%Y")
        rows.append({
            "Indicatore": label,
            "Valore": round(last, 2),
            "Δ": round(delta, 3),
            "Data": date,
        })
    if rows:
        df_out = pd.DataFrame(rows)
        st.dataframe(
            df_out.style.map(
                lambda v: "color: #2ecc71" if isinstance(v, (int, float)) and v > 0
                else ("color: #e74c3c" if isinstance(v, (int, float)) and v < 0 else ""),
                subset=["Δ"]
            ),
            use_container_width=True,
            hide_index=True,
        )


def render_assets_table(prices_summary: dict):
    rows = []
    for ticker, label in ASSETS.items():
        d = prices_summary.get(ticker)
        if not d:
            continue
        rows.append({
            "Asset": label,
            "Close": round(d["close"], 2),
            "1M %": round(d["change_1m"], 2) if d["change_1m"] is not None else None,
            "3M %": round(d["change_3m"], 2) if d["change_3m"] is not None else None,
            "YTD %": round(d["ytd"], 2) if d["ytd"] is not None else None,
            "Data": d["date"],
        })
    if rows:
        df_out = pd.DataFrame(rows)

        def color_pct_cell(v):
            if not isinstance(v, (int, float)) or pd.isna(v):
                return ""
            return "color: #2ecc71" if v > 0 else "color: #e74c3c"

        st.dataframe(
            df_out.style.map(color_pct_cell, subset=["1M %", "3M %", "YTD %"]),
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
        if val > 30:
            r = "🟢 Panico (contrarian bullish)"
        elif val > 20:
            r = "🟡 Stress elevato"
        elif val < 12:
            r = "🔴 Compiacimento estremo"
        elif val < 14:
            r = "🟡 Compiacimento"
        else:
            r = "⚪ Normale"
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
    st.dataframe(
        df.style
          .map(color_num, subset=pct_cols)
          .format({c: lambda v: fmt_pct(v) if isinstance(v, (int, float)) and not pd.isna(v) else "—"
                   for c in pct_cols})
          .format({"Close": lambda v: f"{v:,.0f}" if isinstance(v, (int, float)) else "—"}),
        use_container_width=True,
        hide_index=True,
    )


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
    if cot_df.empty:
        st.warning("COT data non disponibili")
        return

    rows = []
    for _, row in cot_df.iterrows():
        mm_net = row.get("mm_net") or 0
        comm_net = row.get("commercial_net") or 0
        oi = row.get("open_interest") or 0
        mm_pct = (mm_net / oi * 100) if oi > 0 else None
        if mm_pct is not None and mm_pct > 15:
            sent = "🟢 Bullish specs"
        elif mm_pct is not None and mm_pct < -15:
            sent = "🔴 Bearish specs"
        else:
            sent = "🟡 Neutro"
        rows.append({
            "Future": row["label"],
            "Open Interest": f"{int(oi):,}" if oi else "—",
            "Commercial Net": f"{int(comm_net):+,}" if comm_net else "—",
            "Specs Net": f"{int(mm_net):+,}" if mm_net else "—",
            "Sentiment": sent,
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


def render_positioning(phase: str):
    text = get_positioning_recommendation(phase)
    # Rimuovi markup rich, converti in markdown leggibile
    import re
    text = re.sub(r'\[bold green\]', "**", text)
    text = re.sub(r'\[bold red\]', "**", text)
    text = re.sub(r'\[/bold (green|red|white)\]', "**", text)
    text = re.sub(r'\[(green|red|white)\]', "", text)
    text = re.sub(r'\[/(green|red|white)\]', "", text)
    st.markdown(text)


# =============================================================================
# LEGGENDA QUADRANTI CICLO MACRO
# =============================================================================

CYCLE_QUADRANTS = [
    {
        "phase": "goldilocks",
        "emoji": "🟢",
        "label": "GOLDILOCKS",
        "trigger": "CPI < 2,5% · Disoccupazione in calo · Curva positiva",
        "desc": "Il regime ideale: crescita solida, inflazione sotto controllo, mercato del lavoro forte. La Fed è ferma o in taglio. Multipli in espansione.",
        "asset_ok": "Equity growth (Tech, Discretionary) · Small cap · Credit IG",
        "asset_ko": "Cash · Difensivi (Staples, Utilities) · Oro",
        "color": "#2ecc71",
    },
    {
        "phase": "reflation",
        "emoji": "🔵",
        "label": "REFLATION",
        "trigger": "CPI 2–4% · Curva positiva · Crescita in accelerazione",
        "desc": "Ripresa ciclica: domanda in rialzo trascina sia crescita che inflazione. I tassi salgono ma l'economia regge. Ciclici e commodity outperformano.",
        "asset_ok": "Ciclici (Industrials, Financials, Materials) · Energy · Commodity · Small cap value",
        "asset_ko": "Long duration · Staples · Mega cap growth",
        "color": "#3498db",
    },
    {
        "phase": "stagflation",
        "emoji": "🔴",
        "label": "STAGFLATION",
        "trigger": "CPI > 3% · ISM Prices > 70 · Crescita in decelerazione",
        "desc": "Il regime peggiore per i portafogli tradizionali: inflazione ostinatamente alta e crescita che rallenta. La Fed è costretta a stringere anche in frenata.",
        "asset_ok": "Oro · Argento · Energia · Difesa · TIPS / BTP€i · Cash EUR",
        "asset_ko": "Long duration · Consumer Discretionary · REIT · HY credit · Growth tech",
        "color": "#e74c3c",
    },
    {
        "phase": "late_cycle",
        "emoji": "🟡",
        "label": "LATE CYCLE",
        "trigger": "Curva 10Y-2Y < 0 (invertita) · Segnali di decelerazione",
        "desc": "La curva invertita è il segnale storico più affidabile di recessione imminente (lead time medio 12-18 mesi). Il mercato regge ma la qualità conta.",
        "asset_ok": "Quality defensives (Staples, Healthcare) · Oro · Cash short duration",
        "asset_ko": "High beta · Credit HY · Ciclici",
        "color": "#f39c12",
    },
    {
        "phase": "disinflation",
        "emoji": "🟣",
        "label": "DISINFLATION / RECESSIONE",
        "trigger": "CPI < 2% · Disoccupazione in rialzo > +0,3pp in 6m",
        "desc": "Domanda in contrazione, prezzi in discesa, lavoro in indebolimento. La Fed taglia. Bond lunghi outperformano. Oro beneficia dei real rate calanti.",
        "asset_ok": "Long duration treasuries · Quality defensives · Oro",
        "asset_ko": "Ciclici · Commodity · Banche",
        "color": "#9b59b6",
    },
    {
        "phase": "transition",
        "emoji": "⚪",
        "label": "TRANSITION",
        "trigger": "Segnali contrastanti — nessuna regola prevalente",
        "desc": "Fase di ambiguità: i dati macro non convergono verso un regime chiaro. Tipica nei turning point o quando l'economia risponde a shock esogeni.",
        "asset_ok": "Cash per opzionalità · Posizioni esistenti invariate",
        "asset_ko": "Nuovi rischi direzionali forti",
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


def render_cycle_reasoning(phase: str, cpi_yoy, curve_last, unrate_last, unrate_trend, ism_prices_proxy=None):
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
    if ism_prices_proxy is not None:
        if ism_prices_proxy > 70:
            checks.append(f"❌ Prezzi Pagati proxy = **{ism_prices_proxy:.1f}** → > 70 (pressioni inflative forti)")
        elif ism_prices_proxy > 60:
            checks.append(f"🟡 Prezzi Pagati proxy = **{ism_prices_proxy:.1f}** → > 60 (pressioni moderate)")
        elif ism_prices_proxy >= 50:
            checks.append(f"🟡 Prezzi Pagati proxy = **{ism_prices_proxy:.1f}** → in espansione ma contenuto")
        else:
            checks.append(f"✅ Prezzi Pagati proxy = **{ism_prices_proxy:.1f}** → < 50 (prezzi in contrazione)")
        checks.append(f"ℹ️ Proxy = media Philly Fed + NY Fed Prices Paid (scala 0-100, >50 = espansione)")
    else:
        checks.append("⚠️ Prezzi Pagati proxy = **n.d.** (Philly Fed + NY Fed non disponibili)")
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
    # --- Top bar: titolo + ultimo aggiornamento + bottone refresh ---
    col_title, col_info, col_btn = st.columns([5, 3, 2])
    with col_title:
        st.markdown("## 📊 Macro Dashboard")
    with col_info:
        last_update = st.session_state.get("last_update", "—")
        st.markdown(
            f"<div style='padding-top:10px; color:#888; font-size:0.85rem;'>"
            f"Aggiornato: {last_update} &nbsp;·&nbsp; FRED · yfinance · NAAIM · CBOE · CFTC"
            f"</div>",
            unsafe_allow_html=True,
        )
    with col_btn:
        if st.button("🔄 Aggiorna dati", type="primary", use_container_width=True):
            st.session_state["refresh_token"] = st.session_state.get("refresh_token", 0) + 1
            load_all_data.clear()

    # --- Carico dati ---
    refresh_token = st.session_state.get("refresh_token", 0)
    with st.spinner("Carico dati… (prima volta include COT ~30-60s)"):
        (fred_data, prices_summary, prices_raw,
         naaim_df, putcall, sector_df, cot_df, errors) = load_all_data(refresh_token)

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

    # Proxy ISM Prices Paid = media Philly Fed + NY Fed (stessa scala 0-100)
    philly_df = fred_data.get("PPCDFSA066MSFRBPHI", pd.DataFrame())
    ny_df = fred_data.get("PPCDISA066MSFRBNY", pd.DataFrame())
    philly_last = float(philly_df["value"].iloc[-1]) if not philly_df.empty else None
    ny_last = float(ny_df["value"].iloc[-1]) if not ny_df.empty else None
    ism_prices_proxy = round(
        sum(v for v in [philly_last, ny_last] if v is not None)
        / sum(1 for v in [philly_last, ny_last] if v is not None), 1
    ) if any(v is not None for v in [philly_last, ny_last]) else None

    phase, phase_desc = classify_cycle_phase({
        "cpi_yoy": cpi_yoy, "unrate": unrate_last,
        "unrate_trend": unrate_trend, "curve_10y2y": curve_last,
        "ism_prices": ism_prices_proxy,
    })

    render_phase_banner(phase, phase_desc)
    render_cycle_reasoning(phase, cpi_yoy, curve_last, unrate_last, unrate_trend, ism_prices_proxy)
    render_cycle_legend()

    st.divider()

    # --- KPI cards ---
    render_kpi_row(fred_data, prices_summary)

    st.divider()

    # --- Macro + Asset ---
    col_macro, col_asset = st.columns(2)
    with col_macro:
        st.subheader("📋 Indicatori macro (FRED)")
        render_macro_table(fred_data)
    with col_asset:
        st.subheader("💹 Asset & prezzi")
        render_assets_table(prices_summary)

    st.divider()

    # --- Sentiment + Posizionamento ---
    col_sent, col_pos = st.columns([3, 2])
    with col_sent:
        st.subheader("🌡️ Sentiment & regime")
        render_sentiment_table(naaim_df, putcall, prices_summary, prices_raw)
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

    st.divider()

    # --- COT (sempre attivo) ---
    st.subheader("💼 Posizionamento COT")
    render_cot_legend()
    render_cot_table(cot_df)
    st.caption("Sentiment qualitativo (% su open interest). Z-score reali disponibili dopo 3-6 mesi di storico.")


if __name__ == "__main__":
    main()
