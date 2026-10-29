# GEX Calculator + Grafici (servizio unico su Render)

Un solo servizio Render che fa tutto:
- `/`  → app (PWA) con 3 schede: **Manuale**, **Automatico**, **Grafici**
- `/?ticker=AMZN&expiration=AAAA-MM-GG` → dati opzioni (Eulerpool)
- `/verify` → controprova indipendente (FlashAlpha)
- `/analyze` → analisi screenshot multi-timeframe (Gemini), con i livelli GEX calcolati
- `/candles` → candele per il grafico (Yahoo Finance)
- `/nq` → variazione % dei futures NQ in automatico
- `/confluence` → punteggio di confluenza opzioni/NQ (regola: NQ piatto ±0,20% = opzioni 100%, NQ forte ±1% = futures verso il muro)
- `/health` → controllo di stato (per tenere sveglio Render)

Le chiavi API restano solo su Render, mai nel browser.

## Deploy su Render
Build Command:  `pip install -r requirements.txt`
Start Command:  `uvicorn main:app --host 0.0.0.0 --port $PORT`

Environment (Render → Environment):
| Variabile | Obbligatoria | Note |
|---|---|---|
| `EULERPOOL_API_KEY` | sì | dati opzioni |
| `GEMINI_API_KEY` | sì per la scheda Grafici | da Google AI Studio |
| `FLASHALPHA_API_KEY` | no | solo per /verify |
| `GEMINI_MODEL` | no | default `gemini-2.5-flash` |

## Uso
Apri l'indirizzo Render dal telefono → menu del browser → "Aggiungi a schermata Home".
Flusso: scheda Automatico (ticker) → calcola GEX → scheda Grafici → carica gli screenshot → Analizza.
Il piano gratuito di Render si "addormenta": la prima richiesta può metterci circa un minuto.

Strumento informativo: non è consulenza finanziaria.

## Schede
Manuale · Automatico · Grafici (grafico a candele + livelli GEX, NQ auto, confluenza, analisi Gemini) · Diario (analisi salvate sul telefono e verificate a posteriori) · Watchlist (livelli GEX di più ticker).

## Tenere sveglio Render (gratis)
Su uptimerobot.com crea un monitor HTTP(s) su `https://TUO-SERVIZIO.onrender.com/health` con intervallo di 5 minuti.
