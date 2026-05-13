"""
macro_check.py — Macro/sector/COT snapshot on-demand

Uso:
    python macro_check.py              # output a terminale
    python macro_check.py --html       # genera anche report.html
    python macro_check.py --no-cot     # salta COT (più veloce)
    python macro_check.py --quick      # solo prezzi + yield curve

Richiede una FRED API key gratuita: https://fredaccount.stlouisfed.org/apikeys
Metterla in un file .env nella stessa cartella:
    FRED_API_KEY=la_tua_chiave_qui

Lo script salva ogni run in ./data/snapshots/snapshot_YYYYMMDD_HHMM.csv
così nel tempo si accumula uno storico per calcolare percentili reali.
"""

import argparse
import io
import json
import os
import sys
import warnings
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf
from dotenv import load_dotenv
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

warnings.filterwarnings("ignore")
load_dotenv()

console = Console()
FRED_API_KEY = os.getenv("FRED_API_KEY")
DATA_DIR = Path("./data")
SNAPSHOTS_DIR = DATA_DIR / "snapshots"
CACHE_DIR = DATA_DIR / "cache"
for d in [DATA_DIR, SNAPSHOTS_DIR, CACHE_DIR]:
    d.mkdir(parents=True, exist_ok=True)


# =============================================================================
# 1. CONFIGURAZIONE — modifica liberamente
# =============================================================================

# Indicatori FRED (codice → nome leggibile)
FRED_INDICATORS = {
    "T10Y2Y":      "Curva 10Y-2Y",
    "T10Y3M":      "Curva 10Y-3M",
    "CPIAUCSL":    "CPI",
    "CPILFESL":    "Core CPI",
    "PCEPILFE":    "PCE Core",
    "UNRATE":      "Disoccupazione",
    "PAYEMS":      "Non-Farm Payrolls",
    "BAMLH0A0HYM2": "HY credit spread",
    "DFF":         "Fed Funds rate",
    "DGS10":       "Treasury 10Y",
    "PPCDFSA066MSFRBPHI": "Prezzi Pagati (Philly Fed)",
    "PPCDISA066MSFRBNY":  "Prezzi Pagati (NY Fed)",
}

# Asset/sector da yfinance (ticker → label)
ASSETS = {
    "^GSPC":   "S&P 500",
    "^NDX":    "Nasdaq 100",
    "^VIX":    "VIX",
    "DX-Y.NYB": "DXY",
    "GC=F":    "Oro",
    "SI=F":    "Argento",
    "HG=F":    "Rame",
    "CL=F":    "WTI Crude",
    "NG=F":    "Natural Gas",
    "BTC-USD": "Bitcoin",
}

# Sentiment / regime tickers (fetch da yfinance, render in sezione dedicata)
# ^CPC e ^CPCE sono stati rimossi da Yahoo Finance — ora usiamo fetch_putcall_cboe()
SENTIMENT_TICKERS = {}

# NAAIM Exposure Index — fonte ufficiale (CSV pubblico)
NAAIM_URLS = [
    "https://www.naaim.org/wp-content/uploads/2023/01/naaim-exposure-index-data.csv",
    "https://www.naaim.org/wp-content/uploads/2024/01/naaim-exposure-index-data.csv",
    "https://www.naaim.org/wp-content/uploads/2025/01/naaim-exposure-index-data.csv",
]

SECTORS = {
    "XLB":  "Materials",
    "XLC":  "Communications",
    "XLE":  "Energy",
    "XLF":  "Financials",
    "XLI":  "Industrials",
    "XLK":  "Technology",
    "XLP":  "Staples",
    "XLRE": "Real Estate",
    "XLU":  "Utilities",
    "XLV":  "Healthcare",
    "XLY":  "Discretionary",
    "ITA":  "US Defense",
    "URA":  "Uranium",
    "GDX":  "Gold miners",
    "SIL":  "Silver miners",
}

# Contratti COT da estrarre
COT_CONTRACTS = {
    # nome_pulito: (cftc_market_code, report_type)
    "Oro":          ("GOLD - COMMODITY EXCHANGE INC.",          "disaggregated"),
    "Argento":      ("SILVER - COMMODITY EXCHANGE INC.",        "disaggregated"),
    "Rame":         ("COPPER- #1 - COMMODITY EXCHANGE INC.",    "disaggregated"),
    "WTI Crude":    ("CRUDE OIL, LIGHT SWEET - NEW YORK MERCANTILE EXCHANGE", "disaggregated"),
    "Natural Gas":  ("NATURAL GAS - NEW YORK MERCANTILE EXCHANGE", "disaggregated"),
    "E-mini S&P":   ("E-MINI S&P 500 - CHICAGO MERCANTILE EXCHANGE", "financial"),
    "DXY":          ("USD INDEX - ICE FUTURES U.S.",            "financial"),
    "Bitcoin":      ("BITCOIN - CHICAGO MERCANTILE EXCHANGE",   "financial"),
    "VIX":          ("VIX FUTURES - CBOE FUTURES EXCHANGE",     "financial"),
}


# =============================================================================
# 2. FETCHERS
# =============================================================================

def fetch_fred(series_id: str, start: str | None = None) -> pd.DataFrame:
    """Scarica una serie FRED. Ritorna DataFrame con colonne [date, value]."""
    if not FRED_API_KEY:
        raise RuntimeError("FRED_API_KEY mancante. Mettila in .env")
    start = start or (datetime.now() - timedelta(days=365 * 3)).strftime("%Y-%m-%d")
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {
        "series_id": series_id,
        "api_key": FRED_API_KEY,
        "file_type": "json",
        "observation_start": start,
    }
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    obs = r.json().get("observations", [])
    df = pd.DataFrame(obs)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    return df.dropna(subset=["value"])[["date", "value"]].reset_index(drop=True)


def fetch_yfinance_batch(tickers: list[str], period: str = "1y") -> pd.DataFrame:
    """Scarica prezzi in batch. Ritorna DataFrame con MultiIndex columns."""
    df = yf.download(
        tickers, period=period, progress=False, group_by="ticker", auto_adjust=True
    )
    return df


def fetch_naaim() -> pd.DataFrame:
    """
    Scarica NAAIM Exposure Index.
    Primary: tabella HTML (id="surveydata") nella pagina ufficiale — stabile.
    Fallback: XLSX con inception data (URL aggiornato mensilmente da NAAIM).
    """
    from bs4 import BeautifulSoup
    import re as _re

    # --- Primary: tabella HTML embeddada nella pagina ---
    try:
        r = requests.get(
            "https://www.naaim.org/programs/naaim-exposure-index/",
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "lxml")
            table = soup.find("table", {"id": "surveydata"})
            if table:
                rows = table.find_all("tr")
                records = []
                for row in rows[1:]:  # skip header
                    cols = [td.get_text(strip=True) for td in row.find_all("td")]
                    if len(cols) >= 2:
                        records.append({"date": cols[0], "value": cols[1]})
                if records:
                    df = pd.DataFrame(records)
                    df["date"] = pd.to_datetime(df["date"], errors="coerce")
                    df["value"] = pd.to_numeric(df["value"], errors="coerce")
                    df = df.dropna().sort_values("date").reset_index(drop=True)
                    if not df.empty:
                        return df
    except Exception:
        pass

    # --- Fallback: XLSX since-inception (URL cambia ogni mese) ---
    try:
        r = requests.get(
            "https://www.naaim.org/programs/naaim-exposure-index/",
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if r.status_code == 200:
            import re as _re
            xlsx_urls = _re.findall(
                r'https?://[^\s"\'<>]+\.xlsx', r.text, _re.IGNORECASE
            )
            for xlsx_url in xlsx_urls:
                try:
                    rx = requests.get(xlsx_url, timeout=20)
                    if rx.status_code == 200:
                        df = pd.read_excel(io.BytesIO(rx.content))
                        df.columns = [str(c).strip() for c in df.columns]
                        date_col = next(
                            (c for c in df.columns if "date" in c.lower() or "week" in c.lower()),
                            df.columns[0],
                        )
                        val_col = next(
                            (c for c in df.columns if any(k in c.lower() for k in ["mean", "average", "exposure", "number"])),
                            df.columns[1] if len(df.columns) > 1 else None,
                        )
                        if val_col:
                            out = df[[date_col, val_col]].copy()
                            out.columns = ["date", "value"]
                            out["date"] = pd.to_datetime(out["date"], errors="coerce")
                            out["value"] = pd.to_numeric(out["value"], errors="coerce")
                            out = out.dropna().sort_values("date").reset_index(drop=True)
                            if not out.empty:
                                return out
                except Exception:
                    continue
    except Exception:
        pass

    return pd.DataFrame()


def fetch_putcall_cboe() -> dict:
    """
    Scarica i Put/Call ratio correnti dalla pagina statistiche CBOE.
    Ritorna dict con chiavi 'total' e 'equity' (float o None).
    ^CPC e ^CPCE sono stati rimossi da Yahoo Finance.
    """
    import re as _re, json as _json
    try:
        r = requests.get(
            "https://www.cboe.com/us/options/market_statistics/daily/",
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if r.status_code != 200:
            return {"total": None, "equity": None}
        # Next.js serializza il JSON con quote escaped (\\"). Cerchiamo il blocco grezzo.
        m = _re.search(r'ratios\\":\[(.*?)\]', r.text, _re.DOTALL)
        if not m:
            return {"total": None, "equity": None}
        # Ricostruiamo JSON valido dall'escaped string
        raw_escaped = m.group(1)
        raw = "[" + raw_escaped.replace('\\"', '"') + "]"
        ratios = _json.loads(raw)
        result = {"total": None, "equity": None}
        for item in ratios:
            name = item.get("name", "").upper()
            val = item.get("value")
            try:
                val = float(val)
            except (TypeError, ValueError):
                continue
            if "TOTAL" in name and "PUT/CALL" in name:
                result["total"] = val
            elif "EQUITY PUT/CALL" in name:
                result["equity"] = val
        return result
    except Exception:
        return {"total": None, "equity": None}


def fetch_cftc_data(year: int | None = None) -> dict[str, pd.DataFrame]:
    """
    Scarica i tre file COT (disaggregated, legacy, financial) dell'anno richiesto.
    Usa cache locale per non ri-scaricare lo stesso file più volte al giorno.
    """
    year = year or datetime.now().year
    files = {
        "disaggregated": f"https://www.cftc.gov/files/dea/history/fut_disagg_txt_{year}.zip",
        "legacy":        f"https://www.cftc.gov/files/dea/history/deahistfo{year}.zip",
        "financial":     f"https://www.cftc.gov/files/dea/history/fut_fin_txt_{year}.zip",
    }
    result = {}
    for key, url in files.items():
        cache_file = CACHE_DIR / f"cot_{key}_{year}_{datetime.now():%Y%m%d}.csv"
        if cache_file.exists():
            result[key] = pd.read_csv(cache_file, low_memory=False)
            continue
        try:
            r = requests.get(url, timeout=60)
            r.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                txt_name = [n for n in z.namelist() if n.endswith(".txt")][0]
                with z.open(txt_name) as f:
                    df = pd.read_csv(f, low_memory=False)
            df.to_csv(cache_file, index=False)
            result[key] = df
        except Exception as e:
            console.print(f"[yellow]Warning: COT {key} fetch failed: {e}[/yellow]")
            result[key] = pd.DataFrame()
    return result


# =============================================================================
# 3. ANALISI
# =============================================================================

def classify_cycle_phase(macro: dict) -> tuple[str, str]:
    """
    Classifica regime corrente in base a un set di regole semplici.
    Ritorna (phase_code, descrizione lunga).
    """
    ism_prices = macro.get("ism_prices")  # dovrebbe essere input manuale o scraped
    cpi_yoy = macro.get("cpi_yoy")
    unrate = macro.get("unrate")
    unrate_trend = macro.get("unrate_trend")  # delta 6m
    curve = macro.get("curve_10y2y")

    # Regole a priorità decrescente
    if cpi_yoy and cpi_yoy > 3 and ism_prices and ism_prices > 70:
        return ("stagflation", "Stagflazione: inflazione elevata + prezzi pagati in forte espansione")
    if cpi_yoy and cpi_yoy > 3 and (ism_prices is None or ism_prices > 60):
        return ("stagflation", "Stagflazione: CPI > 3% con pressioni sui prezzi elevate")
    if cpi_yoy and cpi_yoy < 2.5 and unrate_trend and unrate_trend < 0:
        return ("goldilocks", "Goldilocks: crescita stabile, inflazione contenuta, lavoro forte")
    if curve and curve < 0:
        return ("late_cycle", "Late cycle: curva invertita, attenzione a deceleration")
    if cpi_yoy and cpi_yoy < 2 and unrate_trend and unrate_trend > 0.3:
        return ("disinflation", "Disinflazione/Recessione: prezzi in calo, lavoro in indebolimento")
    if cpi_yoy and cpi_yoy > 2 and cpi_yoy < 4:
        return ("reflation", "Reflazione: crescita e inflazione in rialzo controllato")
    return ("transition", "Fase di transizione: segnali misti")


def compute_yoy(df: pd.DataFrame) -> float | None:
    """Calcola variazione anno su anno da serie FRED."""
    if df.empty or len(df) < 13:
        return None
    last = df["value"].iloc[-1]
    year_ago_date = df["date"].iloc[-1] - pd.DateOffset(months=12)
    year_ago_row = df[df["date"] <= year_ago_date].tail(1)
    if year_ago_row.empty:
        return None
    year_ago = year_ago_row["value"].iloc[0]
    return ((last / year_ago) - 1) * 100


def compute_sector_metrics(prices: pd.DataFrame, spy_close: pd.Series) -> pd.DataFrame:
    """Per ogni sector, calcola perf 1m/3m/YTD e RS vs SPY."""
    rows = []
    for ticker, label in SECTORS.items():
        if ticker not in prices.columns.get_level_values(0):
            continue
        s = prices[ticker]["Close"].dropna() if "Close" in prices[ticker] else prices[ticker].dropna()
        if len(s) < 60:
            continue
        last = s.iloc[-1]
        # YTD
        ytd_start = s[s.index.year == s.index[-1].year]
        ytd = (last / ytd_start.iloc[0] - 1) * 100 if len(ytd_start) > 0 else None
        # 1m, 3m
        d1m = s.iloc[-22] if len(s) >= 22 else None
        d3m = s.iloc[-66] if len(s) >= 66 else None
        perf_1m = (last / d1m - 1) * 100 if d1m else None
        perf_3m = (last / d3m - 1) * 100 if d3m else None
        # RS vs SPY
        spy_1m = spy_close.iloc[-22] if len(spy_close) >= 22 else None
        spy_3m = spy_close.iloc[-66] if len(spy_close) >= 66 else None
        spy_last = spy_close.iloc[-1]
        rs_1m = perf_1m - ((spy_last / spy_1m - 1) * 100) if (perf_1m and spy_1m) else None
        rs_3m = perf_3m - ((spy_last / spy_3m - 1) * 100) if (perf_3m and spy_3m) else None
        # MA distance
        ma50 = s.tail(50).mean() if len(s) >= 50 else None
        ma200 = s.tail(200).mean() if len(s) >= 200 else None
        pct_ma50 = (last / ma50 - 1) * 100 if ma50 else None
        pct_ma200 = (last / ma200 - 1) * 100 if ma200 else None
        rows.append({
            "ticker": ticker, "label": label, "close": last,
            "perf_1m": perf_1m, "perf_3m": perf_3m, "perf_ytd": ytd,
            "rs_1m": rs_1m, "rs_3m": rs_3m,
            "pct_ma50": pct_ma50, "pct_ma200": pct_ma200,
        })
    return pd.DataFrame(rows).sort_values("rs_1m", ascending=False)


def extract_cot_positioning(cot_data: dict) -> pd.DataFrame:
    """Estrae solo i contratti che ci interessano dai 3 file COT."""
    rows = []
    for label, (market_name, report_type) in COT_CONTRACTS.items():
        df = cot_data.get(report_type, pd.DataFrame())
        if df.empty:
            continue
        # Cerca colonna market_and_exchange (nome varia leggermente tra report)
        name_col = [c for c in df.columns if "Market_and_Exchange" in c or "Market and Exchange" in c]
        if not name_col:
            continue
        match = df[df[name_col[0]].str.strip() == market_name.strip()]
        if match.empty:
            # Prova match parziale
            base = market_name.split(" - ")[0]
            match = df[df[name_col[0]].str.contains(base, case=False, na=False)]
            if match.empty:
                continue
        # Prendi la riga più recente
        date_col = [c for c in df.columns if "Report_Date" in c or "Report Date" in c]
        if date_col:
            match = match.sort_values(date_col[0]).tail(1)
        row = match.iloc[0]
        d = {"label": label, "market": market_name, "report_type": report_type}
        # Mappa colonne diverse a seconda del report type
        if report_type == "disaggregated":
            d["producer_long"] = row.get("Prod_Merc_Positions_Long_All", None)
            d["producer_short"] = row.get("Prod_Merc_Positions_Short_All", None)
            d["swap_long"] = row.get("Swap_Positions_Long_All", None)
            d["swap_short"] = row.get("Swap__Positions_Short_All", None)
            d["mm_long"] = row.get("M_Money_Positions_Long_All", None)
            d["mm_short"] = row.get("M_Money_Positions_Short_All", None)
            d["other_long"] = row.get("Other_Rept_Positions_Long_All", None)
            d["other_short"] = row.get("Other_Rept_Positions_Short_All", None)
            d["mm_net"] = (d["mm_long"] or 0) - (d["mm_short"] or 0)
            d["commercial_net"] = (d["producer_long"] or 0) - (d["producer_short"] or 0)
        elif report_type == "financial":
            d["dealer_long"] = row.get("Dealer_Positions_Long_All", None)
            d["dealer_short"] = row.get("Dealer_Positions_Short_All", None)
            d["asset_mgr_long"] = row.get("Asset_Mgr_Positions_Long_All", None)
            d["asset_mgr_short"] = row.get("Asset_Mgr_Positions_Short_All", None)
            d["lev_fund_long"] = row.get("Lev_Money_Positions_Long_All", None)
            d["lev_fund_short"] = row.get("Lev_Money_Positions_Short_All", None)
            d["mm_net"] = (d["lev_fund_long"] or 0) - (d["lev_fund_short"] or 0)
            d["commercial_net"] = (d["dealer_long"] or 0) - (d["dealer_short"] or 0)
        d["open_interest"] = row.get("Open_Interest_All", None) or row.get("Open Interest (All)", None)
        rows.append(d)
    return pd.DataFrame(rows)


# =============================================================================
# 4. OUTPUT TERMINALE
# =============================================================================

def fmt_num(v, suffix="", decimals=2):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    if abs(v) >= 1_000_000:
        return f"{v/1_000_000:.1f}M{suffix}"
    if abs(v) >= 1_000:
        return f"{v:,.0f}{suffix}".replace(",", ".")
    return f"{v:.{decimals}f}{suffix}".replace(".", ",")


def fmt_pct(v):
    if v is None or pd.isna(v):
        return "—"
    sign = "+" if v > 0 else ""
    return f"{sign}{v:.1f}%".replace(".", ",")


def color_delta(v, good_above=0):
    if v is None or pd.isna(v):
        return "[dim]—[/dim]"
    if v > good_above:
        return f"[green]{fmt_pct(v)}[/green]"
    if v < -good_above:
        return f"[red]{fmt_pct(v)}[/red]"
    return f"[yellow]{fmt_pct(v)}[/yellow]"


def render_macro_section(fred_data: dict, prices_summary: dict):
    """Sezione 1: macro indicators e cycle phase."""
    # Cycle phase
    cpi_yoy = compute_yoy(fred_data.get("CPIAUCSL", pd.DataFrame()))
    unrate_last = fred_data["UNRATE"]["value"].iloc[-1] if not fred_data.get("UNRATE", pd.DataFrame()).empty else None
    unrate_6m_ago = fred_data["UNRATE"]["value"].iloc[-7] if len(fred_data.get("UNRATE", pd.DataFrame())) > 7 else None
    unrate_trend = (unrate_last - unrate_6m_ago) if (unrate_last and unrate_6m_ago) else None
    curve_last = fred_data["T10Y2Y"]["value"].iloc[-1] if not fred_data.get("T10Y2Y", pd.DataFrame()).empty else None

    phase, phase_desc = classify_cycle_phase({
        "cpi_yoy": cpi_yoy,
        "unrate": unrate_last,
        "unrate_trend": unrate_trend,
        "curve_10y2y": curve_last,
        "ism_prices": None,  # non disponibile via FRED facilmente — input manuale
    })

    phase_colors = {
        "goldilocks": "green", "reflation": "blue", "stagflation": "red",
        "late_cycle": "yellow", "disinflation": "magenta", "transition": "white"
    }
    phase_color = phase_colors.get(phase, "white")

    console.print()
    console.print(Panel(
        f"[bold {phase_color}]{phase.upper().replace('_', ' ')}[/bold {phase_color}]\n[white]{phase_desc}[/white]",
        title="📊 Fase del ciclo", border_style=phase_color, expand=False
    ))

    # Tabella indicatori macro
    t = Table(title="Indicatori macro chiave", box=box.SIMPLE_HEAD, header_style="bold")
    t.add_column("Indicatore", style="cyan")
    t.add_column("Valore", justify="right")
    t.add_column("Δ vs precedente", justify="right")
    t.add_column("Data")

    for code, label in FRED_INDICATORS.items():
        df = fred_data.get(code, pd.DataFrame())
        if df.empty or len(df) < 2:
            continue
        last = df["value"].iloc[-1]
        prev = df["value"].iloc[-2]
        delta = last - prev
        delta_pct = (delta / prev * 100) if prev != 0 else None
        date = df["date"].iloc[-1].strftime("%d-%m-%Y")
        delta_str = (
            f"[green]+{delta:.2f}[/green]" if delta > 0
            else f"[red]{delta:.2f}[/red]" if delta < 0
            else f"[white]{delta:.2f}[/white]"
        )
        t.add_row(label, f"{last:.2f}".replace(".", ","), delta_str, date)

    # Aggiungi prezzi
    t.add_row("", "", "", "", style="dim")
    for ticker, label in ASSETS.items():
        if ticker in prices_summary:
            data = prices_summary[ticker]
            t.add_row(
                label,
                fmt_num(data["close"]),
                color_delta(data.get("change_1m"), good_above=0),
                data.get("date", "—")
            )
    console.print(t)

    # Posizionamento suggerito
    pos_text = get_positioning_recommendation(phase)
    console.print(Panel(pos_text, title="🎯 Posizionamento suggerito", border_style=phase_color))


def get_positioning_recommendation(phase: str) -> str:
    """Suggerimenti di posizionamento per regime. Modificali liberamente."""
    recos = {
        "stagflation": (
            "[bold green]Sovrappesare:[/bold green]\n"
            "  • Oro, argento (inflation hedge + central bank buying)\n"
            "  • Energia (XLE) + commodity\n"
            "  • Difesa EU (Rheinmetall, Leonardo, Safran)\n"
            "  • Cash EUR / XEON (opzionalità)\n"
            "  • TIPS / BTP€i\n\n"
            "[bold red]Sottopesare:[/bold red]\n"
            "  • Long duration treasuries\n"
            "  • Consumer discretionary (XLY)\n"
            "  • REIT (XLRE)\n"
            "  • High yield credit\n"
            "  • Growth tech a multipli estremi"
        ),
        "goldilocks": (
            "[bold green]Sovrappesare:[/bold green]\n"
            "  • Equity growth (Tech, Discretionary)\n"
            "  • Small/mid cap\n"
            "  • Credit IG\n\n"
            "[bold red]Sottopesare:[/bold red]\n"
            "  • Cash (drag opportunity cost)\n"
            "  • Difensivi (Staples, Utilities)\n"
            "  • Gold (no urgenza)"
        ),
        "reflation": (
            "[bold green]Sovrappesare:[/bold green]\n"
            "  • Ciclici (Industrials, Financials, Materials)\n"
            "  • Energy + Commodity\n"
            "  • Small cap value\n\n"
            "[bold red]Sottopesare:[/bold red]\n"
            "  • Long duration\n"
            "  • Staples\n"
            "  • Mega cap growth"
        ),
        "late_cycle": (
            "[bold green]Sovrappesare:[/bold green]\n"
            "  • Quality + defensives (Staples, Healthcare)\n"
            "  • Gold\n"
            "  • Cash short duration\n\n"
            "[bold red]Sottopesare:[/bold red]\n"
            "  • High beta\n"
            "  • Credit HY\n"
            "  • Cyclicals"
        ),
        "disinflation": (
            "[bold green]Sovrappesare:[/bold green]\n"
            "  • Long duration treasuries\n"
            "  • Quality defensives\n"
            "  • Gold (real rates calanti)\n\n"
            "[bold red]Sottopesare:[/bold red]\n"
            "  • Cyclicals\n"
            "  • Commodity\n"
            "  • Banks"
        ),
        "transition": "[white]Segnali misti. Mantenere posizioni esistenti, no nuovi rischi direzionali. Aumentare cash per opzionalità.[/white]"
    }
    return recos.get(phase, recos["transition"])


def render_sentiment_section(naaim_df: pd.DataFrame, prices_summary: dict, prices_raw: pd.DataFrame, putcall: dict | None = None):
    """Sezione sentiment & regime indicators: NAAIM, Put/Call, VIX, Copper/Gold, DXY."""
    console.print()

    t = Table(title="🌡️  Sentiment & regime indicators", box=box.SIMPLE_HEAD, header_style="bold")
    t.add_column("Indicatore", style="cyan")
    t.add_column("Valore", justify="right")
    t.add_column("Δ", justify="right")
    t.add_column("Lettura")

    # --- NAAIM Exposure Index (0-200%) -------------------------------------
    if not naaim_df.empty and len(naaim_df) >= 2:
        last = naaim_df["value"].iloc[-1]
        prev = naaim_df["value"].iloc[-2]
        delta = last - prev
        if last >= 90:
            reading = "[red]Estrema esposizione long (contrarian bearish)[/red]"
        elif last >= 70:
            reading = "[yellow]Alta esposizione[/yellow]"
        elif last <= 10:
            reading = "[bold green]Capitulation (contrarian super-bullish)[/bold green]"
        elif last <= 30:
            reading = "[green]Bassa esposizione (contrarian bullish)[/green]"
        else:
            reading = "[white]Neutra[/white]"
        delta_str = (
            f"[green]+{delta:.1f}[/green]" if delta > 0
            else f"[red]{delta:.1f}[/red]" if delta < 0
            else f"[white]{delta:.1f}[/white]"
        )
        date_str = naaim_df["date"].iloc[-1].strftime("%d-%m-%Y")
        t.add_row("NAAIM Exposure", f"{last:.1f}%", delta_str, f"{reading}  [dim]({date_str})[/dim]")
    else:
        t.add_row("NAAIM Exposure", "—", "—", "[dim]fetch fallito (sito NAAIM)[/dim]")

    # --- Put/Call Total (CBOE) ---------------------------------------------
    pc = putcall or {}
    pc_total = pc.get("total")
    if pc_total is not None:
        if pc_total > 1.20:
            reading = "[bold green]Estremo bearish (contrarian bullish)[/bold green]"
        elif pc_total > 1.00:
            reading = "[green]Bearish (precauzionale)[/green]"
        elif pc_total < 0.60:
            reading = "[bold red]Estremo bullish (contrarian bearish)[/bold red]"
        elif pc_total < 0.75:
            reading = "[red]Bullish (compiacimento)[/red]"
        else:
            reading = "[white]Neutro[/white]"
        t.add_row("Put/Call Total (CBOE)", f"{pc_total:.2f}".replace(".", ","), "[dim]today[/dim]", reading)
    else:
        t.add_row("Put/Call Total (CBOE)", "—", "—", "[dim]non disponibile[/dim]")

    # --- Put/Call Equity (CBOE) --------------------------------------------
    pc_equity = pc.get("equity")
    if pc_equity is not None:
        if pc_equity > 0.80:
            reading = "[green]Equity bearish[/green]"
        elif pc_equity < 0.50:
            reading = "[red]Equity bullish (compiacimento)[/red]"
        else:
            reading = "[white]Neutro[/white]"
        t.add_row("Put/Call Equity (CBOE)", f"{pc_equity:.2f}".replace(".", ","), "[dim]today[/dim]", reading)

    # --- VIX ---------------------------------------------------------------
    vix = prices_summary.get("^VIX")
    if vix and vix.get("close") is not None:
        val = vix["close"]
        change = vix.get("change_1m")
        if val > 30:
            reading = "[bold green]Panico (contrarian bullish)[/bold green]"
        elif val > 20:
            reading = "[yellow]Stress elevato[/yellow]"
        elif val < 14:
            reading = "[red]Compiacimento (contrarian bearish)[/red]"
        elif val < 12:
            reading = "[bold red]Compiacimento estremo[/bold red]"
        else:
            reading = "[white]Normale[/white]"
        t.add_row("VIX", f"{val:.2f}".replace(".", ","), color_delta(change), reading)

    # --- Copper/Gold ratio (leading per yields e growth) -------------------
    try:
        if (
            isinstance(prices_raw, pd.DataFrame)
            and "HG=F" in prices_raw.columns.get_level_values(0)
            and "GC=F" in prices_raw.columns.get_level_values(0)
        ):
            copper = prices_raw["HG=F"]["Close"].dropna()
            gold = prices_raw["GC=F"]["Close"].dropna()
            if len(copper) > 0 and len(gold) > 0:
                ratio_now = copper.iloc[-1] / gold.iloc[-1]
                change_3m = None
                if len(copper) >= 66 and len(gold) >= 66:
                    ratio_3m_ago = copper.iloc[-66] / gold.iloc[-66]
                    change_3m = (ratio_now / ratio_3m_ago - 1) * 100
                # Lettura: ratio rising = pro-growth / yields up
                if change_3m is not None and change_3m > 8:
                    reading = "[green]In rialzo: pro-growth, yields support[/green]"
                elif change_3m is not None and change_3m < -8:
                    reading = "[red]In calo: risk-off / growth slowing[/red]"
                else:
                    reading = "[white]Stabile[/white]"
                # Il ratio in valori assoluti è piccolo (~0.0008-0.0012). Lo mostriamo *1000.
                ratio_display = f"{ratio_now*1000:.2f}".replace(".", ",")
                delta_str = color_delta(change_3m) if change_3m is not None else "—"
                t.add_row("Copper/Gold ratio ×1000", ratio_display, delta_str, reading)
    except Exception:
        pass

    # --- DXY (Dollar Index) - trend e livello ------------------------------
    dxy = prices_summary.get("DX-Y.NYB")
    if dxy and dxy.get("close") is not None:
        val = dxy["close"]
        change = dxy.get("change_1m")
        if change is not None and change > 2:
            reading = "[red]In rafforzamento: headwind per oro/EM/commodity[/red]"
        elif change is not None and change < -2:
            reading = "[green]In indebolimento: tailwind per hard asset[/green]"
        else:
            reading = "[white]Lateralizzazione[/white]"
        t.add_row("DXY", f"{val:.2f}".replace(".", ","), color_delta(change), reading)

    console.print(t)


def render_sector_section(sector_df: pd.DataFrame):
    """Sezione 2: rotazione settoriale."""
    console.print()
    if sector_df.empty:
        console.print("[yellow]Sector data non disponibili[/yellow]")
        return

    t = Table(title="📈 Rotazione settoriale (ordinata per RS 1m)", box=box.SIMPLE_HEAD, header_style="bold")
    t.add_column("Sector", style="cyan")
    t.add_column("Ticker", style="dim")
    t.add_column("Close", justify="right")
    t.add_column("1M", justify="right")
    t.add_column("3M", justify="right")
    t.add_column("YTD", justify="right")
    t.add_column("RS 1M", justify="right")
    t.add_column("vs MA200", justify="right")

    for _, row in sector_df.iterrows():
        t.add_row(
            row["label"],
            row["ticker"],
            fmt_num(row["close"]),
            color_delta(row["perf_1m"]),
            color_delta(row["perf_3m"]),
            color_delta(row["perf_ytd"]),
            color_delta(row["rs_1m"], good_above=1),
            color_delta(row["pct_ma200"]),
        )
    console.print(t)

    # Top/bottom 3
    top = sector_df.head(3)["label"].tolist()
    bottom = sector_df.tail(3)["label"].tolist()
    console.print(
        f"[green]▲ Capitale in ingresso:[/green] {', '.join(top)}\n"
        f"[red]▼ Capitale in uscita:[/red] {', '.join(bottom)}"
    )


def render_cot_section(cot_df: pd.DataFrame):
    """Sezione 3: posizionamento COT."""
    console.print()
    if cot_df.empty:
        console.print("[yellow]COT data non disponibili (skip con --no-cot per evitare)[/yellow]")
        return

    t = Table(title="💼 Posizionamento COT (ultima release)", box=box.SIMPLE_HEAD, header_style="bold")
    t.add_column("Future", style="cyan")
    t.add_column("Open Interest", justify="right")
    t.add_column("Commercial Net", justify="right")
    t.add_column("Speculators Net", justify="right")
    t.add_column("Sentiment")

    for _, row in cot_df.iterrows():
        mm_net = row.get("mm_net", 0) or 0
        comm_net = row.get("commercial_net", 0) or 0
        oi = row.get("open_interest", 0) or 0
        # Sentiment qualitativo (placeholder — sostituire con z-score appena hai storico)
        if oi > 0:
            mm_pct = mm_net / oi * 100
            if mm_pct > 15:
                sent = "[green]Bullish specs[/green]"
            elif mm_pct < -15:
                sent = "[red]Bearish specs[/red]"
            else:
                sent = "[yellow]Neutro[/yellow]"
        else:
            sent = "[dim]—[/dim]"

        t.add_row(
            row["label"],
            f"{int(oi):,}".replace(",", ".") if oi else "—",
            f"{int(comm_net):+,}".replace(",", ".") if comm_net else "—",
            f"{int(mm_net):+,}".replace(",", ".") if mm_net else "—",
            sent,
        )
    console.print(t)
    console.print(
        "[dim]Note: il sentiment qui è qualitativo (% su open interest). "
        "Dopo 3-6 mesi di run accumulati avrai dati per z-score reali.[/dim]"
    )


# =============================================================================
# 5. SNAPSHOT E STORICO
# =============================================================================

def save_snapshot(fred_data: dict, prices_summary: dict, sector_df: pd.DataFrame, cot_df: pd.DataFrame, naaim_df: pd.DataFrame = None, prices_raw: pd.DataFrame = None):
    """Salva uno snapshot per accumulare storico nel tempo."""
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    rows = []
    # Macro
    for code, df in fred_data.items():
        if df.empty:
            continue
        rows.append({"timestamp": ts, "category": "macro", "key": code,
                     "value": df["value"].iloc[-1], "obs_date": df["date"].iloc[-1].isoformat()})
    # Prices
    for ticker, data in prices_summary.items():
        rows.append({"timestamp": ts, "category": "price", "key": ticker,
                     "value": data["close"], "obs_date": data.get("date", "")})
    # NAAIM
    if naaim_df is not None and not naaim_df.empty:
        rows.append({"timestamp": ts, "category": "sentiment", "key": "NAAIM",
                     "value": naaim_df["value"].iloc[-1],
                     "obs_date": naaim_df["date"].iloc[-1].isoformat()})
    # Copper/Gold ratio
    try:
        if (isinstance(prices_raw, pd.DataFrame) and
            "HG=F" in prices_raw.columns.get_level_values(0) and
            "GC=F" in prices_raw.columns.get_level_values(0)):
            copper = prices_raw["HG=F"]["Close"].dropna()
            gold = prices_raw["GC=F"]["Close"].dropna()
            if len(copper) > 0 and len(gold) > 0:
                rows.append({"timestamp": ts, "category": "ratio", "key": "copper_gold",
                             "value": copper.iloc[-1] / gold.iloc[-1], "obs_date": ""})
    except Exception:
        pass
    # Sectors RS
    for _, row in sector_df.iterrows():
        rows.append({"timestamp": ts, "category": "sector_rs_1m", "key": row["ticker"],
                     "value": row["rs_1m"], "obs_date": ""})
    # COT
    for _, row in cot_df.iterrows():
        rows.append({"timestamp": ts, "category": "cot_mm_net", "key": row["label"],
                     "value": row.get("mm_net", 0), "obs_date": ""})
        rows.append({"timestamp": ts, "category": "cot_commercial_net", "key": row["label"],
                     "value": row.get("commercial_net", 0), "obs_date": ""})

    df = pd.DataFrame(rows)
    out = SNAPSHOTS_DIR / f"snapshot_{ts}.csv"
    df.to_csv(out, index=False)
    console.print(f"\n[dim]💾 Snapshot salvato: {out}[/dim]")


# =============================================================================
# 6. MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-cot", action="store_true", help="Salta COT (più veloce)")
    parser.add_argument("--quick", action="store_true", help="Solo macro + prezzi (skip sectors e COT)")
    parser.add_argument("--html", action="store_true", help="Genera anche report HTML")
    args = parser.parse_args()

    console.print(Panel(
        "[bold]Macro / Sector / COT — Snapshot[/bold]\n"
        f"[dim]{datetime.now().strftime('%A %d %B %Y, %H:%M')}[/dim]",
        border_style="blue"
    ))

    fred_data = {}
    prices_summary = {}
    sector_df = pd.DataFrame()
    cot_df = pd.DataFrame()
    naaim_df = pd.DataFrame()
    prices = pd.DataFrame()
    putcall = {"total": None, "equity": None}

    with Progress(SpinnerColumn(), TextColumn("{task.description}"), transient=True) as progress:
        # FRED
        task = progress.add_task("Scarico dati FRED...", total=None)
        for code in FRED_INDICATORS:
            try:
                fred_data[code] = fetch_fred(code)
            except Exception as e:
                console.print(f"[yellow]FRED {code} failed: {e}[/yellow]")
        progress.remove_task(task)

        # Prezzi (asset + sectors + sentiment tickers)
        task = progress.add_task("Scarico prezzi (yfinance)...", total=None)
        all_tickers = (
            list(ASSETS.keys())
            + list(SECTORS.keys())
            + list(SENTIMENT_TICKERS.keys())
            + ["SPY"]
        )
        try:
            prices = fetch_yfinance_batch(all_tickers, period="1y")
            for ticker in all_tickers:
                if ticker in prices.columns.get_level_values(0):
                    s = prices[ticker]["Close"].dropna()
                    if not s.empty:
                        last = s.iloc[-1]
                        d1m = s.iloc[-22] if len(s) >= 22 else None
                        change_1m = ((last / d1m - 1) * 100) if d1m else None
                        prices_summary[ticker] = {
                            "close": last,
                            "change_1m": change_1m,
                            "date": s.index[-1].strftime("%d-%m-%Y"),
                        }
            spy_close = prices["SPY"]["Close"].dropna() if "SPY" in prices.columns.get_level_values(0) else pd.Series()
        except Exception as e:
            console.print(f"[red]Prezzi fallback: {e}[/red]")
            prices = pd.DataFrame()
            spy_close = pd.Series()
        progress.remove_task(task)

        # NAAIM
        task = progress.add_task("Scarico NAAIM Exposure Index...", total=None)
        try:
            naaim_df = fetch_naaim()
        except Exception as e:
            console.print(f"[yellow]NAAIM failed: {e}[/yellow]")
        progress.remove_task(task)

        # Put/Call ratio (CBOE)
        task = progress.add_task("Scarico Put/Call ratio (CBOE)...", total=None)
        try:
            putcall = fetch_putcall_cboe()
        except Exception as e:
            console.print(f"[yellow]Put/Call CBOE failed: {e}[/yellow]")
        progress.remove_task(task)

        # Sectors
        if not args.quick and not prices.empty:
            task = progress.add_task("Calcolo metriche settoriali...", total=None)
            try:
                sector_df = compute_sector_metrics(prices, spy_close)
            except Exception as e:
                console.print(f"[yellow]Sectors failed: {e}[/yellow]")
            progress.remove_task(task)

        # COT
        if not args.no_cot and not args.quick:
            task = progress.add_task("Scarico CFTC COT data (può richiedere 30-60s)...", total=None)
            try:
                cot_raw = fetch_cftc_data()
                cot_df = extract_cot_positioning(cot_raw)
            except Exception as e:
                console.print(f"[yellow]COT failed: {e}[/yellow]")
            progress.remove_task(task)

    # Render
    render_macro_section(fred_data, prices_summary)
    render_sentiment_section(naaim_df, prices_summary, prices, putcall)
    if not args.quick:
        render_sector_section(sector_df)
        if not args.no_cot:
            render_cot_section(cot_df)

    # Snapshot per storico
    save_snapshot(fred_data, prices_summary, sector_df, cot_df, naaim_df, prices)

    # HTML opzionale
    if args.html:
        console.print("[yellow]HTML output non ancora implementato — todo[/yellow]")

    console.print("\n[bold green]✓ Done.[/bold green]")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted[/yellow]")
        sys.exit(1)
