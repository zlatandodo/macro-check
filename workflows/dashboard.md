# Workflow: Dashboard macro interattiva

## Obiettivo
Avviare una dashboard web locale che visualizza in tempo reale il quadro
macro/settoriale/COT e permette di aggiornare i dati on-demand.

## Prerequisiti
- Virtual environment attivo con tutte le dipendenze installate
- File `.env` con `FRED_API_KEY` nella root del progetto

## Come avviare

```bash
# Dalla cartella macro-check, con il venv attivo
venv/bin/streamlit run tools/dashboard.py
```

Il browser si apre automaticamente su `http://localhost:8501`.
Per fermare: `Ctrl+C` nel terminale.

## Come aggiornare i dati
Clicca il pulsante **"🔄 Aggiorna dati"** nella sidebar.
I dati vengono ri-fetchati freschi da tutte le fonti (FRED, yfinance, NAAIM, CBOE).
Il fetch COT è opzionale (spunta in sidebar) — richiede 30-60s in più.

## Layout dashboard
1. **Header** — fase ciclo corrente con colore regime
2. **KPI cards** — VIX, DXY, Curva 10Y-2Y, Fed Funds (aggiornamento immediato)
3. **Macro FRED** — tabella indicatori con delta vs precedente
4. **Prezzi asset** — tabella prezzi con performance 1M
5. **Sentiment** — NAAIM, Put/Call CBOE, Copper/Gold, DXY lettura
6. **Rotazione settoriale** — bar chart RS 1M + tabella completa
7. **COT** — tabella posizionamento (se abilitato in sidebar)

## Fonti dati
| Dato | Fonte | Frequenza aggiornamento |
|---|---|---|
| Macro (CPI, NFP, curve…) | FRED API | Dipende dalla serie (mensile/giornaliero) |
| Prezzi asset + settori | Yahoo Finance | Real-time durante mercato aperto |
| NAAIM Exposure Index | naaim.org | Settimanale (mercoledì) |
| Put/Call ratio | CBOE | Giornaliero |
| COT | CFTC | Settimanale (venerdì) |

## Troubleshooting
- **Porta 8501 occupata**: aggiungi `--server.port 8502` al comando
- **FRED_API_KEY mancante**: controlla che `.env` esista nella root del progetto
- **COT lento**: prima chiamata scarica ~10MB di ZIP; usa cache locale successivamente
- **NAAIM vuoto**: il sito NAAIM è a volte instabile — riprova tra qualche minuto
