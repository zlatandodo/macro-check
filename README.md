# macro_check.py

Script Python standalone per snapshot rapido del quadro macro, rotazione settoriale e posizionamento COT.

## Setup (5 minuti, una volta sola)

```bash
# 1. Crea cartella e venv
mkdir macro-check && cd macro-check
python3 -m venv venv
source venv/bin/activate  # su macOS/Linux
# venv\Scripts\activate   # su Windows

# 2. Copia macro_check.py, requirements.txt, .env.example in questa cartella

# 3. Installa dipendenze
pip install -r requirements.txt

# 4. Prendi una FRED API key gratuita su https://fredaccount.stlouisfed.org/apikeys
#    Bastano 30 secondi: ti registri, copi la chiave

# 5. Crea il file .env
cp .env.example .env
# poi apri .env e incolla la tua FRED API key
```

## Uso

```bash
# Snapshot completo (macro + sectors + COT) — circa 30-60 secondi
python macro_check.py

# Veloce: solo macro + prezzi, no sectors né COT (~10 sec)
python macro_check.py --quick

# Senza COT (utile durante la settimana, COT cambia solo il venerdì)
python macro_check.py --no-cot
```

## Output

Lo script stampa nel terminale quattro sezioni:

1. **Fase del ciclo** con classificazione automatica (Goldilocks / Reflation / Stagflation / Late cycle / Disinflation / Transition) + indicatori macro chiave + posizionamento suggerito
2. **Sentiment & regime indicators** — NAAIM Exposure Index, Put/Call ratio (Total ed Equity), VIX, Copper/Gold ratio, DXY trend — con lettura contestualizzata (es. NAAIM > 90 = contrarian bearish, VIX < 14 = compiacimento, Copper/Gold in rialzo = pro-growth)
3. **Rotazione settoriale** con tabella RS, performance 1M/3M/YTD per 15 ETF (11 S&P sectors + Defense + Uranium + Gold/Silver miners)
4. **Posizionamento COT** sui 9 future principali (SPY, Oro, Argento, Rame, WTI, NatGas, DXY, BTC, VIX) con net positions di smart money vs speculators

Ogni run salva uno snapshot in `./data/snapshots/` per accumulare storico nel tempo.

## Cosa modificare quando ne hai voglia

- **Asset/ticker:** edita i dizionari `FRED_INDICATORS`, `ASSETS`, `SECTORS`, `COT_CONTRACTS` in cima allo script
- **Soglie classificazione regime:** edita `classify_cycle_phase()`
- **Suggerimenti di posizionamento:** edita `get_positioning_recommendation()`
- **Aggiungere indicatori:** basta aggiungere il codice FRED e il nome nel dizionario, lo script li pesca automaticamente

## Limiti noti / TODO

- I percentili veri (es. "ISM Prices al 95° percentile") richiedono storico: per ora lo script usa solo soglie qualitative. Dopo 3-6 mesi di run accumulati avrai abbastanza dati per calcolare z-score reali.
- ISM dettaglio (Prices, Employment, New Orders) non è disponibile via FRED API in modo pulito — vanno inseriti manualmente o aggiunto uno scraper dedicato.
- L'output HTML è stub (`--html` esiste ma non è implementato). Se serve, è facile aggiungere usando `jinja2` o anche solo string templating.
- COT è solo "ultima settimana" — per vedere trend serve sviluppare un grafico dei net positions su 52 settimane (richiede storico locale).

## Estensioni future facili da aggiungere

- **Alert Telegram** quando un indicatore va in zona estrema (es. ISM < 47 o curve re-invertita): un bot in 30 righe
- **Calendario release dati** macro (FRED ha le date di rilascio nei metadati delle serie)
- **Esportazione CSV** pulita di tutti gli snapshot accumulati per analisi in Portfolio Performance
- **Watchlist personali** (es. portafoglio difesa EU) con check di RS

## Quick run setup completo (copia-incolla)

```bash
mkdir macro-check && cd macro-check
python3 -m venv venv && source venv/bin/activate
# copia i 3 file nella cartella
pip install -r requirements.txt
echo "FRED_API_KEY=incollala_qui" > .env
python macro_check.py
```
