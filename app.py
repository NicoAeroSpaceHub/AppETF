"""
Backend de données pour ETF PEA Optimizer.

Remplace le bricolage "proxy CORS public + Yahoo Finance non officiel"
utilisé côté navigateur par un vrai service : le calcul des historiques
se fait ici, côté serveur, avec la librairie `yfinance`. Aucun problème
de CORS puisque c'est votre backend qui contacte Yahoo, pas le navigateur
de l'utilisateur.

Ce fichier sert aussi directement la page d'interface (index.html) à la
racine "/" : une fois déployé (ex. sur Render), une seule URL suffit,
utilisable depuis n'importe quel navigateur (y compris Safari iPhone),
sans avoir besoin de renseigner l'adresse du backend séparément.

Lancement local (sur un ordinateur) :
    pip install -r requirements.txt
    uvicorn app:app --reload --port 8000
puis ouvrez http://localhost:8000 dans un navigateur.

Déploiement (ex. Render, pour un usage 100% depuis iPhone) : voir
README.md — tout se fait par navigateur, sans terminal.

Endpoints :
    GET  /                          -> sert la page d'interface (index.html)
    GET  /api/metrics?isin=...      -> métriques pour un seul ISIN
    POST /api/metrics/batch         -> métriques pour une liste d'ISIN
                                        body: {"isins": ["FR001400U5Q4", ...]}
    GET  /api/search?q=...          -> recherche de fonds par nom/ISIN (Yahoo)
    GET  /api/metrics-by-symbol     -> métriques pour un ticker Yahoo direct

IMPORTANT — ce que Yahoo Finance / yfinance NE fournissent PAS : l'éligibilité
PEA, la politique de distribution (capitalisant/distribuant) et la
disponibilité sur un courtier donné (Trade Republic ou autre) ne sont pas
des données financières générales — ce sont des informations réglementaires
françaises et des choix commerciaux de courtier, absentes de toute API
financière généraliste. La recherche ci-dessous renvoie donc des candidats
avec de vraies données de performance, mais l'utilisateur doit confirmer
lui-même ces trois points avant d'ajouter un fonds à son univers.
"""
from __future__ import annotations

import math
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import requests
import yfinance as yf
import pandas as pd
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

app = FastAPI(title="ETF PEA Optimizer — Backend de données", version="1.0")

# CORS ouvert : usage local / démonstration. Si vous déployez ce backend
# publiquement, restreignez allow_origins à votre propre domaine.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# index.html doit se trouver dans le même dossier que app.py.
_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")


@app.get("/", response_class=HTMLResponse)
def serve_frontend():
    """Sert la page d'interface directement, pour n'avoir qu'une seule
    URL à ouvrir (pratique depuis un iPhone, sans backend à renseigner)."""
    with open(_HTML_PATH, "r", encoding="utf-8") as f:
        return f.read()

# Tickers Yahoo Finance vérifiés manuellement (recherche web à la date de
# rédaction) pour certaines parts d'accumulation citées dans l'application.
# Pour tout ISIN absent de cette table, le backend tente une résolution
# automatique via l'API de recherche de Yahoo Finance (voir resolve_symbol).
# À COMPLÉTER/CORRIGER vous-même si un ticker devient obsolète : les codes
# ISIN et tickers d'ETF peuvent changer (fusions de parts, changements de
# fournisseur de données, etc.).
KNOWN_TICKERS: dict[str, str] = {
    "FR001400U5Q4": "DCAM.PA",   # Amundi PEA Monde (MSCI World)
    "IE0002XZSHO1": "WPEA.PA",   # iShares MSCI World Swap PEA
    "FR0011550185": "ESE.PA",    # BNP Paribas Easy S&P 500
    "FR0013412285": "PSP5.PA",   # Amundi PEA S&P 500 Screened
    "FR0011871110": "PUST.PA",   # Amundi PEA Nasdaq-100
    "FR0013412020": "PAEEM.PA",  # Amundi PEA Emerging Markets ESG
    "FR0013380607": "CACC.PA",   # Amundi CAC 40
}

# Cache mémoire très simple pour éviter de re-télécharger l'historique à
# chaque clic pendant la même session du backend.
_cache: dict[str, tuple[float, dict]] = {}
CACHE_TTL_SECONDS = 30 * 60  # 30 minutes


class ISINList(BaseModel):
    isins: list[str]


def resolve_symbol(isin: str) -> Optional[str]:
    """Résout un ISIN vers un symbole Yahoo Finance : table connue en
    priorité, sinon recherche via l'API (non officielle mais appelée ici
    côté serveur, donc sans souci de CORS)."""
    if isin in KNOWN_TICKERS:
        return KNOWN_TICKERS[isin]
    try:
        r = requests.get(
            "https://query2.finance.yahoo.com/v1/finance/search",
            params={"q": isin, "quotesCount": 5, "newsCount": 0},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=6,
        )
        r.raise_for_status()
        quotes = r.json().get("quotes", [])
        if quotes:
            return quotes[0].get("symbol")
    except Exception:
        return None
    return None


def compute_full_metrics(symbol: str) -> dict:
    """Calcule, à partir d'un seul téléchargement d'historique (5 ans,
    hebdomadaire) :
      - rendement et volatilité annualisés sur 1 an, 3 ans et 5 ans
        (rendements logarithmiques hebdomadaires, comme avant)
      - le dernier cours de clôture connu et sa date
      - une détection best-effort de la politique de distribution,
        basée sur l'historique réel des dividendes versés (voir
        `detect_distribution_policy` ci-dessous)
    """
    tkr = yf.Ticker(symbol)
    hist = tkr.history(period="5y", interval="1wk", auto_adjust=True)
    if hist.empty:
        raise ValueError(f"aucun historique renvoyé par Yahoo Finance pour {symbol}")
    closes = hist["Close"].dropna()
    if len(closes) < 15:
        raise ValueError(f"historique insuffisant pour {symbol} ({len(closes)} points)")

    def annualized(sub) -> tuple[Optional[float], Optional[float]]:
        if len(sub) < 8:
            return None, None
        log_rets = sub.pct_change().dropna().apply(lambda x: math.log1p(x))
        if log_rets.empty:
            return None, None
        mean_w = float(log_rets.mean())
        var_w = float(log_rets.var())
        ann_ret = (math.exp(mean_w * 52) - 1) * 100
        ann_vol = math.sqrt(max(var_w, 0.0) * 52) * 100
        return round(ann_ret, 2), round(ann_vol, 2)

    last_date = closes.index[-1]
    out: dict = {"points": int(len(closes))}
    for label, years in (("1y", 1), ("3y", 3), ("5y", 5)):
        cutoff = last_date - pd.Timedelta(days=int(years * 365.25))
        sub = closes[closes.index >= cutoff]
        r, v = annualized(sub)
        out[f"ret_{label}"] = r
        out[f"vol_{label}"] = v
    # Alias par défaut (rétro-compatibilité avec le reste de l'app) : 3 ans.
    out["ret"] = out["ret_3y"]
    out["vol"] = out["vol_3y"]

    # Dernier cours réel (non ajusté des dividendes) sur les derniers jours.
    try:
        daily = tkr.history(period="5d", interval="1d", auto_adjust=False)
        daily_closes = daily["Close"].dropna()
        if not daily_closes.empty:
            out["last_price"] = round(float(daily_closes.iloc[-1]), 4)
            out["last_price_date"] = daily_closes.index[-1].strftime("%Y-%m-%d")
        else:
            out["last_price"] = round(float(closes.iloc[-1]), 4)
            out["last_price_date"] = last_date.strftime("%Y-%m-%d")
    except Exception:
        out["last_price"] = round(float(closes.iloc[-1]), 4)
        out["last_price_date"] = last_date.strftime("%Y-%m-%d")

    dist_policy, dist_basis = detect_distribution_policy(tkr, symbol)
    out["dist_policy"] = dist_policy
    out["dist_basis"] = dist_basis
    return out


def _name_hint(tkr: "yf.Ticker", symbol: str) -> Optional[str]:
    """Cherche 'Acc'/'Dist' (ou variantes) dans le nom du fonds ou son
    ticker — convention quasi systématique chez les émetteurs UCITS."""
    text = symbol.upper()
    try:
        info = tkr.info or {}
        text += " " + str(info.get("longName") or "") .upper()
        text += " " + str(info.get("shortName") or "").upper()
    except Exception:
        pass
    acc_markers = (" ACC", "(ACC)", "-ACC", "ACCUM", " CAP ", "(C)")
    dist_markers = (" DIST", "(DIST)", "-DIST", "DISTRIB", "(D)")
    if any(m in text for m in dist_markers):
        return "Distribuant"
    if any(m in text for m in acc_markers):
        return "Capitalisant"
    return None


def detect_distribution_policy(tkr: "yf.Ticker", symbol: str) -> tuple[str, str]:
    """Déduit (sans garantie) si un fonds est capitalisant ou distribuant en
    croisant deux indices indépendants :
      1. le nom/ticker du fonds, qui contient presque toujours une mention
         "Acc"/"Dist" (convention standard des émetteurs UCITS) ;
      2. l'historique réel des dividendes versés (un fonds capitalisant n'en
         verse jamais, un distribuant en verse périodiquement).
    Limite connue : un fonds distribuant très récemment lancé peut n'avoir
    versé aucun dividende et sembler "capitalisant" à tort si son nom ne
    donne pas d'indice non plus — la donnée reste une indication, pas une
    certitude absolue à traiter comme telle."""
    name_hint = _name_hint(tkr, symbol)

    try:
        divs = tkr.dividends
    except Exception:
        divs = None

    div_hint = None
    div_detail = ""
    if divs is not None and len(divs) > 0:
        try:
            last_div_date = divs.index[-1]
            now = pd.Timestamp.now(tz=last_div_date.tz) if last_div_date.tzinfo else pd.Timestamp.now()
            days_since = (now - last_div_date).days
        except Exception:
            days_since = 0
        if days_since < 800:
            div_hint = "Distribuant"
            div_detail = f"dernier dividende versé le {last_div_date.strftime('%Y-%m-%d')}"
        else:
            div_hint = "Distribuant (ancien)"
            div_detail = f"dividendes versés par le passé (dernier le {last_div_date.strftime('%Y-%m-%d')}) — vérifiez si la politique a changé"
    else:
        div_hint = "Capitalisant"
        div_detail = "aucun dividende versé dans l'historique disponible"

    div_hint_simple = "Distribuant" if div_hint.startswith("Distribuant") else "Capitalisant"

    if name_hint and name_hint == div_hint_simple:
        return name_hint, f"nom du fonds ET historique de dividendes concordants ({div_detail})"
    if name_hint and name_hint != div_hint_simple:
        return f"{name_hint} (signaux contradictoires)", f"le nom suggère {name_hint} mais {div_detail} — à vérifier sur la fiche DIC"
    if div_hint:
        return div_hint, div_detail
    return "Indéterminé", "aucun indice exploitable (ni nom, ni historique de dividendes)"


def fetch_one(isin: str) -> dict:
    now = time.time()
    cached = _cache.get(isin)
    if cached and (now - cached[0]) < CACHE_TTL_SECONDS:
        return cached[1]

    symbol = resolve_symbol(isin)
    if not symbol:
        result = {"isin": isin, "symbol": None, "ret": None, "vol": None,
                   "error": "symbole Yahoo Finance introuvable pour cet ISIN"}
        _cache[isin] = (now, result)
        return result

    try:
        metrics = compute_full_metrics(symbol)
        result = {"isin": isin, "symbol": symbol, "error": None, **metrics}
    except Exception as exc:
        result = {"isin": isin, "symbol": symbol, "ret": None, "vol": None,
                   "error": str(exc)}

    _cache[isin] = (now, result)
    return result


@app.get("/api/health")
def health():
    return {"status": "ok", "message": "Backend ETF PEA Optimizer actif — voir /docs pour la documentation interactive."}


@app.get("/api/metrics")
def metrics_single(isin: str):
    return fetch_one(isin)


@app.post("/api/metrics/batch")
def metrics_batch(payload: ISINList):
    results: list[dict] = []
    # Requêtes en parallèle (I/O bound) pour ne pas attendre 3-4s x 14 ETF
    # en séquentiel.
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(fetch_one, isin): isin for isin in payload.isins}
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:
                results.append({"isin": futures[future], "error": str(exc)})
    return {"results": results}


@app.get("/api/search")
def search_funds(q: str):
    """Recherche de fonds par nom ou ISIN via l'API de recherche de Yahoo
    Finance. Ne renvoie QUE ce que Yahoo sait : ticker, nom, place de
    cotation, devise. Ne dit RIEN sur l'éligibilité PEA, la politique de
    distribution, ni la disponibilité sur un courtier — à vérifier
    vous-même (fiche DIC de l'émetteur, app du courtier, justETF...)."""
    try:
        r = requests.get(
            "https://query2.finance.yahoo.com/v1/finance/search",
            params={"q": q, "quotesCount": 10, "newsCount": 0},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=6,
        )
        r.raise_for_status()
        quotes = r.json().get("quotes", [])
    except Exception as exc:
        return {"results": [], "error": str(exc)}

    results = []
    for item in quotes:
        qtype = item.get("quoteType")
        if qtype not in ("ETF", "EQUITY", "MUTUALFUND"):
            continue
        results.append({
            "symbol": item.get("symbol"),
            "name": item.get("longname") or item.get("shortname") or item.get("symbol"),
            "exchange": item.get("exchange"),
            "type": qtype,
        })
    return {"results": results}


@app.get("/api/metrics-by-symbol")
def metrics_by_symbol(symbol: str):
    """Comme /api/metrics mais à partir d'un ticker Yahoo Finance déjà
    connu (issu de /api/search), sans passer par la résolution ISIN."""
    now = time.time()
    cache_key = f"symbol:{symbol}"
    cached = _cache.get(cache_key)
    if cached and (now - cached[0]) < CACHE_TTL_SECONDS:
        return cached[1]
    try:
        metrics = compute_full_metrics(symbol)
        currency = None
        try:
            fi = yf.Ticker(symbol).fast_info
            currency = fi.get("currency") if hasattr(fi, "get") else getattr(fi, "currency", None)
        except Exception:
            currency = None
        result = {"symbol": symbol, "currency": currency, "error": None, **metrics}
    except Exception as exc:
        result = {"symbol": symbol, "error": str(exc)}
    _cache[cache_key] = (now, result)
    return result


@app.get("/api/history")
def get_history(symbol: str = None, isin: str = None, range: str = "3y", interval: str = "1wk"):
    """Historique de cours complet pour un fonds — utilisé pour le
    graphique de prix dans le temps (clic sur un nom de fonds), avec
    superposition possible de plusieurs fonds côté frontend. Accepte soit
    un ticker Yahoo direct (`symbol`), soit un ISIN à résoudre (`isin`)."""
    if not symbol and isin:
        symbol = resolve_symbol(isin)
    if not symbol:
        return {"symbol": None, "dates": [], "closes": [], "error": "aucun symbole fourni ou résolu"}

    allowed_ranges = {"1y", "3y", "5y", "10y", "max"}
    allowed_intervals = {"1d", "1wk", "1mo"}
    if range not in allowed_ranges:
        range = "3y"
    if interval not in allowed_intervals:
        interval = "1wk"
    cache_key = f"history:{symbol}:{range}:{interval}"
    now = time.time()
    cached = _cache.get(cache_key)
    if cached and (now - cached[0]) < CACHE_TTL_SECONDS:
        return cached[1]
    try:
        hist = yf.Ticker(symbol).history(period=range, interval=interval, auto_adjust=True)
        closes = hist["Close"].dropna()
        if closes.empty:
            raise ValueError("aucun historique disponible")
        result = {
            "symbol": symbol,
            "dates": [d.strftime("%Y-%m-%d") for d in closes.index],
            "closes": [round(float(c), 4) for c in closes.values],
            "error": None,
        }
    except Exception as exc:
        result = {"symbol": symbol, "dates": [], "closes": [], "error": str(exc)}
    _cache[cache_key] = (now, result)
    return result
