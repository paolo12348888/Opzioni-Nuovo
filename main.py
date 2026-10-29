"""
GEX Calculator - Proxy per Render
Fa da ponte tra la PWA (che non puo' chiamare direttamente le API per via
del CORS) ed Eulerpool (dati principali) + FlashAlpha (verifica indipendente
opzionale). Le API key restano qui, sul server, mai visibili nel browser.

Endpoint:
  GET /?ticker=AMZN&expiration=2026-10-02[&debug=1]
      -> dati opzioni pronti per il calcolatore (fonte: Eulerpool)
  GET /verify?ticker=AMZN&expiration=2026-10-02[&debug=1]
      -> Net GEX, Call/Put Wall, Zero Gamma, Max Pain da FlashAlpha,
         per confronto indipendente con quanto calcola la PWA

DEPLOY SU RENDER:
1. Carica questa cartella in un repository GitHub (separato da Investitor Plus)
2. Su render.com -> New -> Web Service -> connetti quel repository
3. Build Command:  pip install -r requirements.txt
   Start Command:  uvicorn main:app --host 0.0.0.0 --port $PORT
4. Environment -> Add Environment Variable:
   EULERPOOL_API_KEY = la tua chiave da eulerpool.com
   FLASHALPHA_API_KEY = la tua chiave da flashalpha (opzionale, solo per /verify)
5. Deploy. Copia l'URL che Render ti assegna nel campo "Indirizzo del
   servizio automatico" della PWA.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import eulerpool
import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

app = FastAPI(title="GEX Calculator Proxy")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

EULERPOOL_API_KEY = os.environ.get("EULERPOOL_API_KEY")
FLASHALPHA_API_KEY = os.environ.get("FLASHALPHA_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

BASE_DIR = Path(__file__).parent


# ---------------------------------------------------------------------------
# Endpoint principale: dati per il calcolatore (Eulerpool)
# ---------------------------------------------------------------------------

@app.get("/")
def get_options(
    ticker: Optional[str] = Query(None, description="Es. AMZN"),
    expiration: Optional[str] = Query(None, description="YYYY-MM-DD, opzionale"),
    debug: Optional[int] = Query(None, description="Se 1, restituisce la risposta grezza di Eulerpool"),
):
    # Senza ticker, "/" serve l'app (PWA): app e dati vivono sullo stesso indirizzo.
    if not ticker:
        return FileResponse(BASE_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    if not EULERPOOL_API_KEY:
        raise HTTPException(status_code=500, detail="EULERPOOL_API_KEY non configurata su Render")

    try:
        with eulerpool.Eulerpool(EULERPOOL_API_KEY) as client:
            raw = None
            if expiration:
                try:
                    raw = client.market.options(ticker, expiration=expiration)
                except TypeError:
                    pass
                if raw is None:
                    try:
                        raw = client.get(f"/market/options/{ticker}", expiration=expiration)
                    except Exception:
                        raw = None
            if raw is None:
                raw = client.market.options(ticker)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Errore chiamata Eulerpool: {e}")

    if debug:
        return debug_view(raw)

    return transform(raw, ticker, expiration)


def debug_view(raw):
    def to_jsonable(obj, depth=0):
        if depth > 4:
            return str(obj)
        if isinstance(obj, (str, int, float, bool)) or obj is None:
            return obj
        if isinstance(obj, dict):
            return {k: to_jsonable(v, depth + 1) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [to_jsonable(v, depth + 1) for v in obj[:5]]
        if hasattr(obj, "model_dump"):
            return to_jsonable(obj.model_dump(), depth + 1)
        if hasattr(obj, "dict"):
            return to_jsonable(obj.dict(), depth + 1)
        if hasattr(obj, "__dict__"):
            return to_jsonable(vars(obj), depth + 1)
        return str(obj)

    try:
        return {"type": str(type(raw)), "content": to_jsonable(raw)}
    except Exception as e:
        return {"type": str(type(raw)), "error": f"Non serializzabile: {e}", "str_preview": str(raw)[:2000]}


def transform(raw: dict, ticker: str, expiration_filter: Optional[str]) -> dict:
    now = datetime.now(timezone.utc)

    exp_blocks = raw.get("expirations") or []
    if expiration_filter:
        filtered = [b for b in exp_blocks if str(b.get("expiration_date", "")).startswith(expiration_filter)]
        if filtered:
            exp_blocks = filtered

    options = []
    put_oi_total = 0.0
    call_oi_total = 0.0
    spot_estimates = []

    for block in exp_blocks:
        exp_date_str = block.get("expiration_date")
        try:
            exp_date = datetime.fromisoformat(exp_date_str)
        except (ValueError, TypeError):
            continue
        dte = max(1, (exp_date - now.replace(tzinfo=None)).days)

        calls_by_strike = {}
        puts_by_strike = {}

        for c in block.get("calls", []):
            try:
                strike = float(c["strike"])
                oi = float(c.get("open_interest") or 0)
                iv = float(c.get("implied_vol") or 0)
            except (KeyError, ValueError, TypeError):
                continue
            calls_by_strike[strike] = c
            if iv > 0:
                options.append({"strike": strike, "option_type": "call", "open_interest": oi, "implied_vol": iv, "days_to_expiry": dte})
                call_oi_total += oi

        for p in block.get("puts", []):
            try:
                strike = float(p["strike"])
                oi = float(p.get("open_interest") or 0)
                iv = float(p.get("implied_vol") or 0)
            except (KeyError, ValueError, TypeError):
                continue
            puts_by_strike[strike] = p
            if iv > 0:
                options.append({"strike": strike, "option_type": "put", "open_interest": oi, "implied_vol": iv, "days_to_expiry": dte})
                put_oi_total += oi

        for strike, c in calls_by_strike.items():
            p = puts_by_strike.get(strike)
            if not p:
                continue
            c_mid = mid_price(c)
            p_mid = mid_price(p)
            if c_mid is not None and p_mid is not None:
                spot_estimates.append(strike + c_mid - p_mid)

    spot = median(spot_estimates) if spot_estimates else None

    return {
        "ticker": ticker.upper(),
        "spot": spot,
        "spot_source": "stimato da parita' put-call (nessun prezzo diretto nella risposta Eulerpool)" if spot else None,
        "put_oi_total": put_oi_total,
        "call_oi_total": call_oi_total,
        "options": options,
        "expirations_used": [b.get("expiration_date") for b in exp_blocks],
        "total_expirations_available": raw.get("totalExpirations"),
    }


def mid_price(option: dict) -> Optional[float]:
    try:
        bid = float(option.get("bid") or 0)
        ask = float(option.get("ask") or 0)
        if bid > 0 and ask > 0:
            return (bid + ask) / 2
        last = float(option.get("last_price") or 0)
        return last if last > 0 else None
    except (TypeError, ValueError):
        return None


def median(values):
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


# ---------------------------------------------------------------------------
# Endpoint di verifica indipendente: FlashAlpha
# ---------------------------------------------------------------------------

@app.get("/verify")
def verify_independent(
    ticker: str = Query(..., description="Es. AMZN"),
    expiration: Optional[str] = Query(None, description="YYYY-MM-DD, opzionale"),
    debug: Optional[int] = Query(None, description="Se 1, restituisce la risposta grezza di FlashAlpha"),
):
    """
    Interroga FlashAlpha (fonte indipendente da Eulerpool) per confrontare
    Net GEX, Call/Put Wall, Zero Gamma e Max Pain. Piano gratuito: 5
    chiamate al giorno -- usalo con parsimonia, non per il monitoraggio
    continuo.
    """
    if not FLASHALPHA_API_KEY:
        raise HTTPException(status_code=500, detail="FLASHALPHA_API_KEY non configurata su Render")

    if not expiration:
        raise HTTPException(
            status_code=400,
            detail="Il piano gratuito di FlashAlpha richiede sempre una scadenza specifica. "
                   "Aggiungi &expiration=YYYY-MM-DD alla richiesta (nella PWA: compila il campo "
                   "'Scadenza' prima di premere Verifica)."
        )

    try:
        import flashalpha
        fa = flashalpha.FlashAlpha(FLASHALPHA_API_KEY)
        raw = fa.gex(ticker, expiration=expiration)
    except Exception as e:
        err_text = str(e)
        if "0DTE" in err_text or "Growth plan" in err_text:
            if "same-day" in err_text or "0DTE" in err_text:
                raise HTTPException(
                    status_code=400,
                    detail="FlashAlpha (piano gratuito) non supporta la verifica su scadenze 0DTE "
                           "(che scadono oggi stesso). Riprova con una scadenza futura, es. la "
                           "prossima weekly o la monthly che stai seguendo."
                )
        raise HTTPException(status_code=502, detail=f"Errore chiamata FlashAlpha: {e}")

    if debug:
        return debug_view(raw)

    return transform_flashalpha(raw, ticker)


def transform_flashalpha(raw, ticker: str) -> dict:
    """
    NOTA: schema di risposta FlashAlpha non testato dal vivo (rete
    sandbox non raggiunge le loro API). Questa funzione prova diversi
    nomi di campo plausibili in base alla documentazione pubblica; se il
    primo test non torna, usa ?debug=1 per vedere la struttura reale e
    la sistemiamo insieme in due minuti, come gia' fatto per Eulerpool.
    """
    if not isinstance(raw, dict):
        if hasattr(raw, "model_dump"):
            raw = raw.model_dump()
        elif hasattr(raw, "__dict__"):
            raw = vars(raw)
        else:
            raw = {}

    def pick(*names):
        for n in names:
            if n in raw and raw[n] is not None:
                return raw[n]
        return None

    call_wall = pick("call_wall", "callWall")
    put_wall = pick("put_wall", "putWall")
    max_pain = pick("max_pain", "maxPain")

    # Se FlashAlpha non fornisce i muri gia' pronti, proviamo a ricavarli
    # da un eventuale elenco per-strike nella risposta (stessa logica che
    # usiamo nel nostro calcolatore: strike con GEX call piu' alto/put piu'
    # basso in valore assoluto).
    strikes_list = pick("strikes", "by_strike", "strike_data")
    if isinstance(strikes_list, list) and strikes_list:
        best_call, best_call_strike = None, None
        best_put, best_put_strike = None, None
        for s in strikes_list:
            if not isinstance(s, dict):
                continue
            k = s.get("strike")
            cg = s.get("call_gex") or s.get("callGex")
            pg = s.get("put_gex") or s.get("putGex")
            if k is None:
                continue
            if cg is not None and (best_call is None or cg > best_call):
                best_call, best_call_strike = cg, k
            if pg is not None and (best_put is None or pg < best_put):
                best_put, best_put_strike = pg, k
        if call_wall is None and best_call_strike is not None:
            call_wall = best_call_strike
        if put_wall is None and best_put_strike is not None:
            put_wall = best_put_strike

        # Max Pain richiede l'Open Interest per strike (non il GEX):
        # proviamo diversi nomi di campo plausibili prima di arrenderci.
        if max_pain is None:
            candidates = []
            for s in strikes_list:
                if isinstance(s, dict) and s.get("strike") is not None:
                    candidates.append(s["strike"])
            best_strike, best_pain = None, None
            for candidate in candidates:
                pain = 0.0
                has_oi = False
                for s in strikes_list:
                    if not isinstance(s, dict):
                        continue
                    k = s.get("strike")
                    if k is None:
                        continue
                    call_oi = s.get("call_oi") or s.get("callOi") or s.get("call_open_interest")
                    put_oi = s.get("put_oi") or s.get("putOi") or s.get("put_open_interest")
                    if call_oi is not None:
                        has_oi = True
                        if candidate > k:
                            pain += (candidate - k) * call_oi
                    if put_oi is not None:
                        has_oi = True
                        if candidate < k:
                            pain += (k - candidate) * put_oi
                if not has_oi:
                    break  # FlashAlpha non espone l'OI su questo endpoint: inutile continuare
                if best_pain is None or pain < best_pain:
                    best_pain, best_strike = pain, candidate
            if best_strike is not None:
                max_pain = best_strike

    return {
        "ticker": ticker.upper(),
        "source": "FlashAlpha",
        "net_gex": pick("net_gex", "netGex", "gex", "total_gex"),
        "gamma_flip": pick("gamma_flip", "gammaFlip", "zero_gamma"),
        "call_wall": call_wall,
        "put_wall": put_wall,
        "max_pain": max_pain,
        "_raw_keys_found": list(raw.keys()) if raw else [],
        "_strikes_sample": strikes_list[:2] if isinstance(strikes_list, list) else None,
    }


@app.api_route("/health", methods=["GET", "HEAD"])
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# File statici della PWA (stesso dominio dell'API: niente CORS, niente URL da configurare)
# ---------------------------------------------------------------------------

@app.get("/manifest.json", include_in_schema=False)
def static_manifest():
    return FileResponse(BASE_DIR / "manifest.json", media_type="application/manifest+json")


@app.get("/sw.js", include_in_schema=False)
def static_sw():
    return FileResponse(BASE_DIR / "sw.js", media_type="application/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/icon-192.png", include_in_schema=False)
def static_icon_192():
    return FileResponse(BASE_DIR / "icon-192.png", media_type="image/png")


@app.get("/icon-512.png", include_in_schema=False)
def static_icon_512():
    return FileResponse(BASE_DIR / "icon-512.png", media_type="image/png")


@app.get("/icon-maskable.png", include_in_schema=False)
def static_icon_maskable():
    return FileResponse(BASE_DIR / "icon-maskable.png", media_type="image/png")


@app.get("/icon.svg", include_in_schema=False)
def static_icon():
    return FileResponse(BASE_DIR / "icon.svg", media_type="image/svg+xml")


# ---------------------------------------------------------------------------
# Analisi screenshot multi-timeframe (Gemini) con i livelli GEX gia' calcolati
# ---------------------------------------------------------------------------

class ChartImage(BaseModel):
    tf: str
    data: str  # JPEG in base64, senza prefisso "data:"


class AnalyzeRequest(BaseModel):
    asset: str = ""
    horizon: str = ""
    note: str = ""
    nq_change: Optional[float] = None  # variazione % di NQ oggi, opzionale
    gex: Optional[dict] = None         # livelli calcolati dalla scheda GEX
    images: list[ChartImage]


def build_prompt(req: AnalyzeRequest, nq=None, conf=None) -> str:
    righe = [f"Immagine {i + 1}: time frame {im.tf}" for i, im in enumerate(req.images)]
    g = req.gex or {}
    if g:
        regime = ("positivo: regime stabilizzante, tendenza a rispettare i muri"
                  if (g.get("netGex") or 0) > 0
                  else "negativo: regime piu' volatile, i movimenti possono amplificarsi")
        gex_txt = (
            f"Livelli opzioni calcolati (GEX): spot {g.get('spot')}, Call Wall {g.get('callWall')}, "
            f"Put Wall {g.get('putWall')}, Zero Gamma {g.get('zeroGamma')}, Max Pain {g.get('maxPain')}, "
            f"Net GEX {g.get('netGex')} ({regime})."
        )
    else:
        gex_txt = "Livelli GEX non disponibili: analizza solo la price action e dichiaralo tra le avvertenze."

    if nq is None:
        nq_txt = "Variazione di NQ non disponibile: non applicare la regola sui futures e dichiaralo."
    else:
        nq_txt = f"NQ: {nq:+.2f}%."
    if conf:
        nq_txt += (f"\nConfluenza calcolata dal sistema (trattala come dato di partenza, motiva se il grafico la conferma o la smentisce): "
                   f"punteggio {conf['score']:+.0f} su 100 ({conf['label']}). {conf['note']}")

    return (
        f"Sei un analista tecnico. Asset: \"{req.asset or 'non specificato'}\". Orizzonte: {req.horizon or 'swing'}.\n"
        + "\n".join(righe)
        + f"\nNote dell'utente: {req.note or 'nessuna'}\n\n{gex_txt}\n{nq_txt}\n\n"
        "Regola di confluenza dell'utente: se NQ e' piatto (entro +/-0,20%) le opzioni del titolo contano al 100% "
        "e il prezzo tende a rispettare i muri; se NQ e' forte (circa +/-1% o piu') i futures fanno da propulsore "
        "verso il muro successivo (Call Wall se sale, Put Wall se scende); nelle zone intermedie pesa in modo graduale.\n\n"
        "Leggi dagli screenshot struttura di mercato (massimi/minimi), trend, supporti/resistenze, candele, gap, "
        "volatilita' e indicatori visibili. Confronta i time frame. Posiziona lo spot rispetto ai livelli GEX. "
        "Usa solo cio' che vedi, se un dato non e' leggibile dillo. Sii prudente, nessuna certezza. "
        "Rispondi SOLO con JSON valido in italiano, senza backtick, con questo schema:\n"
        '{"bias_generale":"Rialzista|Ribassista|Neutrale","confidenza":0-100,"allineamento":"alto|medio|basso",'
        '"sintesi":"2-3 frasi","confluenza_gex":"1-3 frasi su come opzioni, NQ e grafico si combinano",'
        '"timeframe":[{"tf":"","bias":"Rialzista|Ribassista|Neutrale","nota":""}],"supporti":[""],"resistenze":[""],'
        '"scenari":[{"nome":"","probabilita":0-100,"descrizione":"","invalidazione":""}],"avvertenze":[""]}\n'
        "Le probabilita' degli scenari sommano a 100."
    )


@app.post("/analyze")
def analyze(req: AnalyzeRequest):
    if not GEMINI_API_KEY:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY non configurata su Render")
    if not req.images:
        raise HTTPException(status_code=400, detail="Nessuna immagine ricevuta")
    if len(req.images) > 8:
        raise HTTPException(status_code=400, detail="Massimo 8 immagini per analisi")
    if sum(len(i.data) for i in req.images) > 14_000_000:
        raise HTTPException(status_code=413, detail="Immagini troppo pesanti: usane meno o riduci la risoluzione")

    parts = [{"inline_data": {"mime_type": "image/jpeg", "data": im.data}} for im in req.images]
    nq = req.nq_change
    nq_info = None
    if nq is None:
        nq_info = fetch_nq()
        nq = nq_info["change_pct"] if nq_info else None
    conf = compute_confluence(req.gex, nq) if req.gex else None
    parts.append({"text": build_prompt(req, nq, conf)})
    body = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.3},
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

    try:
        r = httpx.post(url, json=body, headers={"x-goog-api-key": GEMINI_API_KEY}, timeout=120)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Errore di rete verso Gemini: {e}")

    if r.status_code != 200:
        try:
            msg = r.json().get("error", {}).get("message", r.text[:300])
        except Exception:
            msg = r.text[:300]
        raise HTTPException(status_code=502, detail=f"Gemini ha risposto {r.status_code}: {msg}")

    try:
        cand = r.json()["candidates"][0]
        text = "".join(p.get("text", "") for p in cand["content"]["parts"])
        text = text.replace("```json", "").replace("```", "")
        result = json.loads(text[text.index("{"): text.rindex("}") + 1])
        result["confluenza_numerica"] = conf
        result["nq"] = nq_info or ({"change_pct": nq} if nq is not None else None)
        return result
    except Exception:
        raise HTTPException(status_code=502, detail="Risposta di Gemini vuota, bloccata dai filtri o non in formato JSON. Riprova.")


# ---------------------------------------------------------------------------
# Candele per il grafico (dati Yahoo Finance, endpoint pubblico non ufficiale)
# ---------------------------------------------------------------------------

CANDLE_MAP = {
    "5m": ("5m", "5d"),
    "15m": ("15m", "1mo"),
    "30m": ("30m", "1mo"),
    "1h": ("60m", "3mo"),
    "1D": ("1d", "1y"),
    "1W": ("1wk", "5y"),
}


@app.get("/candles")
def candles(ticker: str = Query(..., description="Es. AMZN"), tf: str = Query("1h")):
    if tf not in CANDLE_MAP:
        raise HTTPException(status_code=400, detail=f"Time frame non supportato: {tf}")
    interval, rng = CANDLE_MAP[tf]
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker.upper()}"
    try:
        r = httpx.get(
            url,
            params={"interval": interval, "range": rng, "includePrePost": "false"},
            headers={"User-Agent": "Mozilla/5.0 (compatible; GEXCalc/1.0)"},
            timeout=20,
        )
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Errore di rete verso la fonte prezzi: {e}")
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"La fonte prezzi ha risposto {r.status_code} (ticker errato o limite di richieste). Riprova tra poco.")
    try:
        res = r.json()["chart"]["result"][0]
        ts = res["timestamp"]
        q = res["indicators"]["quote"][0]
        out = []
        for i, t in enumerate(ts):
            o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
            if None in (o, h, l, c):
                continue
            out.append({"time": t, "open": round(o, 4), "high": round(h, 4), "low": round(l, 4), "close": round(c, 4)})
    except Exception:
        raise HTTPException(status_code=502, detail="Nessun dato prezzi disponibile per questo ticker/time frame")
    if not out:
        raise HTTPException(status_code=502, detail="Nessuna candela disponibile (mercato chiuso o ticker non valido)")
    return {"ticker": ticker.upper(), "tf": tf, "candles": out}


# ---------------------------------------------------------------------------
# NQ automatico + confluenza opzioni/futures (regola dell'utente, calcolata nel codice)
# ---------------------------------------------------------------------------

YF_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; GEXCalc/1.0)"}


def fetch_nq() -> Optional[dict]:
    """Variazione % dei futures Nasdaq (NQ=F) rispetto alla chiusura precedente. None se la fonte non risponde."""
    try:
        r = httpx.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/NQ=F",
            params={"interval": "1d", "range": "5d"},
            headers=YF_HEADERS,
            timeout=15,
        )
        if r.status_code != 200:
            return None
        res = r.json()["chart"]["result"][0]
        closes = [c for c in res["indicators"]["quote"][0]["close"] if c is not None]
        price = res["meta"].get("regularMarketPrice") or closes[-1]
        prev = closes[-2] if len(closes) >= 2 else None
        if not prev:
            return None
        return {"price": round(price, 2), "prev_close": round(prev, 2), "change_pct": round((price / prev - 1) * 100, 3)}
    except Exception:
        return None


def compute_confluence(g: dict, nq: Optional[float]) -> Optional[dict]:
    """
    Regola: NQ piatto (+/-0,20%) -> opzioni del titolo al 100% (il prezzo rispetta i muri);
    NQ forte (+/-1%) -> i futures spingono verso il muro successivo; in mezzo, peso graduale.
    Punteggio da -100 (ribassista) a +100 (rialzista).
    """
    try:
        spot, call, put = float(g["spot"]), float(g["callWall"]), float(g["putWall"])
    except (KeyError, TypeError, ValueError):
        return None
    if call <= put:
        return None
    net = float(g.get("netGex") or 0)

    pos = min(1.0, max(0.0, (spot - put) / (call - put)))
    s_opt = (0.5 - pos) * 200  # +100 sul Put Wall (rimbalzo), -100 sul Call Wall (resistenza)
    if net < 0:
        s_opt *= 0.5  # regime negativo: i muri tengono meno

    if nq is None:
        wf, s_fut, state = 0.0, 0.0, "non disponibile"
    else:
        a = abs(nq)
        wf = 0.0 if a <= 0.20 else min(1.0, (a - 0.20) / 0.80)
        s_fut = max(-100.0, min(100.0, nq * 100))
        state = "piatto" if a <= 0.20 else ("forte" if a >= 1.0 else "intermedio")

    score = (1 - wf) * s_opt + wf * s_fut
    label = "Rialzista" if score >= 25 else ("Ribassista" if score <= -25 else "Neutrale")
    if label == "Rialzista":
        target_name, target = "Call Wall", call
    elif label == "Ribassista":
        target_name, target = "Put Wall", put
    else:
        target_name, target = None, None

    if nq is None:
        note = "NQ non disponibile: punteggio basato solo sulle opzioni."
    elif state == "piatto":
        note = "NQ piatto: contano al 100% le opzioni del titolo; il prezzo tende a rispettare i muri (resistenza vicino al Call Wall, supporto vicino al Put Wall)."
    elif state == "forte":
        note = "NQ forte: i futures fanno da propulsore verso il muro successivo."
    else:
        note = f"NQ intermedio: peso opzioni {round((1 - wf) * 100)}%, futures {round(wf * 100)}%."
    if net < 0:
        note += " Net GEX negativo: i muri sono meno affidabili, movimenti piu' ampi."

    return {
        "score": round(score, 1),
        "label": label,
        "nq_change": nq,
        "nq_state": state,
        "w_options": round((1 - wf) * 100),
        "w_futures": round(wf * 100),
        "pos_in_range": round(pos, 3),
        "target_name": target_name,
        "target": target,
        "target_dist_pct": round((target / spot - 1) * 100, 2) if target else None,
        "note": note,
    }


@app.get("/nq")
def get_nq():
    info = fetch_nq()
    if not info:
        raise HTTPException(status_code=502, detail="NQ non disponibile al momento (fonte prezzi non raggiungibile). Inserisci il valore a mano.")
    return info


class ConfluenceReq(BaseModel):
    gex: dict
    nq_change: Optional[float] = None


@app.post("/confluence")
def confluence(req: ConfluenceReq):
    nq, nq_info = req.nq_change, None
    if nq is None:
        nq_info = fetch_nq()
        nq = nq_info["change_pct"] if nq_info else None
    c = compute_confluence(req.gex, nq)
    if not c:
        raise HTTPException(status_code=400, detail="Livelli GEX incompleti: calcola prima il GEX (servono spot, Call Wall e Put Wall)")
    return {"confluenza": c, "nq": nq_info or ({"change_pct": nq} if nq is not None else None)}


# ---------------------------------------------------------------------------
# Importazione option chain da screenshot di Barchart (lettura con Gemini)
# ---------------------------------------------------------------------------

class ParseOptionsReq(BaseModel):
    images: list[ChartImage]          # tf non usato qui, riusiamo lo stesso modello
    expiration: Optional[str] = None  # YYYY-MM-DD, se vuoi forzarla


PARSE_PROMPT = (
    "Leggi gli screenshot di una tabella option chain (di solito Barchart). Estrai SOLO cio' che e' visibile, senza inventare numeri. "
    "La tabella puo' avere Calls a sinistra, Strike al centro e Puts a destra sulla stessa riga (allora produci due righe per strike), "
    "oppure due tabelle separate. Colonne tipiche: Latest/Last, Bid, Ask, Mid, Volume, Open Int (Open Interest), IV (percentuale). "
    "Togli i separatori delle migliaia. Valori mancanti, '-' o 'N/A' -> null. L'IV va riportata come numero percentuale senza il simbolo % "
    "(es. 32.45). Se vedi il prezzo del sottostante riportalo in underlying_price. Se vedi la data di scadenza riportala come YYYY-MM-DD in expiration. "
    "Ignora le greche e qualunque altra colonna. Se una riga e' tagliata o illeggibile, saltala e segnalalo in notes. "
    "Rispondi SOLO con JSON valido, senza backtick, con questo schema:\n"
    '{"underlying_price":number|null,"expiration":"YYYY-MM-DD"|null,'
    '"rows":[{"strike":number,"type":"call|put","open_interest":number|null,"iv_percent":number|null,"volume":number|null,"last":number|null,"mid":number|null}],'
    '"notes":[""]}'
)


def gemini_json(parts: list) -> dict:
    body = {"contents": [{"role": "user", "parts": parts}], "generationConfig": {"responseMimeType": "application/json", "temperature": 0}}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    try:
        r = httpx.post(url, json=body, headers={"x-goog-api-key": GEMINI_API_KEY}, timeout=120)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Errore di rete verso Gemini: {e}")
    if r.status_code != 200:
        try:
            msg = r.json().get("error", {}).get("message", r.text[:300])
        except Exception:
            msg = r.text[:300]
        raise HTTPException(status_code=502, detail=f"Gemini ha risposto {r.status_code}: {msg}")
    try:
        text = "".join(p.get("text", "") for p in r.json()["candidates"][0]["content"]["parts"])
        text = text.replace("```json", "").replace("```", "")
        return json.loads(text[text.index("{"): text.rindex("}") + 1])
    except Exception:
        raise HTTPException(status_code=502, detail="Gemini non ha restituito dati leggibili. Riprova con screenshot piu' nitidi.")


def _num(v):
    try:
        if v is None or v == "":
            return None
        return float(str(v).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def normalize_rows(raw: dict, expiration_override: Optional[str] = None) -> dict:
    warnings = list(raw.get("notes") or [])
    exp = expiration_override or raw.get("expiration")
    dte = None
    if exp:
        try:
            d = datetime.strptime(str(exp)[:10], "%Y-%m-%d").date()
            dte = max(1, (d - datetime.now(timezone.utc).date()).days)
        except ValueError:
            warnings.append(f"Scadenza non valida ({exp}): inseriscila a mano nel formato AAAA-MM-GG.")
            exp = None
    if dte is None:
        warnings.append("Scadenza non riconosciuta: compila il campo 'Scadenza' per calcolare i giorni alla scadenza.")

    best, dropped = {}, 0
    for row in raw.get("rows") or []:
        strike = _num(row.get("strike"))
        typ = str(row.get("type", "")).lower().strip()
        oi = _num(row.get("open_interest"))
        iv = _num(row.get("iv_percent"))
        if strike is None or typ not in ("call", "put") or oi is None or iv is None or iv <= 0:
            dropped += 1
            continue
        iv = iv / 100 if iv > 3 else iv  # percentuale -> decimale (Barchart mostra es. 32.45)
        if not (0.03 <= iv <= 3.0):
            warnings.append(f"IV insolita a strike {strike:g} {typ} ({iv * 100:.1f}%): controlla la riga.")
        item = {"strike": strike, "type": typ, "oi": oi, "iv": round(iv, 4),
                "volume": _num(row.get("volume")), "last": _num(row.get("last")), "mid": _num(row.get("mid"))}
        key = (strike, typ)
        if key not in best or sum(v is not None for v in item.values()) > sum(v is not None for v in best[key].values()):
            best[key] = item  # righe duplicate (screenshot sovrapposti): tieni la piu' completa

    items = sorted(best.values(), key=lambda x: (x["strike"], x["type"]))
    if dropped:
        warnings.append(f"{dropped} {'riga scartata' if dropped == 1 else 'righe scartate'} perche' incompleta/e (mancano OI o IV).")
    n_call = sum(1 for i in items if i["type"] == "call")
    n_put = len(items) - n_call
    if items and (n_call == 0 or n_put == 0):
        warnings.append("Ci sono solo " + ("put" if n_call == 0 else "call") + ": aggiungi anche l'altro lato per un GEX affidabile.")
    if items and all(i["oi"] == 0 for i in items):
        warnings.append("Tutti gli Open Interest risultano 0: probabile lettura errata.")

    def f(v):
        return "" if v is None else (str(int(v)) if float(v).is_integer() else str(v))

    lines = [",".join([f(i["strike"]), i["type"], f(i["oi"]), str(i["iv"]), str(dte if dte is not None else ""), f(i["volume"]), f(i["last"]), f(i["mid"])]) for i in items]
    return {"spot": _num(raw.get("underlying_price")), "expiration": exp, "dte": dte, "rows_count": len(items),
            "calls": n_call, "puts": n_put, "csv": "\n".join(lines), "warnings": warnings}


@app.post("/parse-options")
def parse_options(req: ParseOptionsReq):
    if not GEMINI_API_KEY:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY non configurata su Render")
    if not req.images:
        raise HTTPException(status_code=400, detail="Nessuno screenshot ricevuto")
    if len(req.images) > 8:
        raise HTTPException(status_code=400, detail="Massimo 8 screenshot per volta")
    if sum(len(i.data) for i in req.images) > 14_000_000:
        raise HTTPException(status_code=413, detail="Immagini troppo pesanti: usane meno")
    parts = [{"inline_data": {"mime_type": "image/jpeg", "data": im.data}} for im in req.images]
    parts.append({"text": PARSE_PROMPT})
    out = normalize_rows(gemini_json(parts), req.expiration)
    if not out["rows_count"]:
        raise HTTPException(status_code=422, detail="Non ho trovato righe utilizzabili negli screenshot (servono Strike, Open Interest e IV visibili).")
    return out
