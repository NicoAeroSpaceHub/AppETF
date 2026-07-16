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


def compute_metrics(symbol: str) -> dict:
    """Télécharge l'historique hebdomadaire sur 3 ans via yfinance et
    calcule un rendement et une volatilité annualisés à partir des
    rendements logarithmiques hebdomadaires — méthode identique à celle
    utilisée côté frontend pour rester cohérent."""
    hist = yf.Ticker(symbol).history(period="3y", interval="1wk", auto_adjust=True)
    if hist.empty:
        raise ValueError(f"aucun historique renvoyé par Yahoo Finance pour {symbol}")
    closes = hist["Close"].dropna()
    if len(closes) < 30:
        raise ValueError(f"historique insuffisant pour {symbol} ({len(closes)} points)")

    log_returns = closes.pct_change().dropna().apply(lambda x: math.log1p(x))
    mean_w = float(log_returns.mean())
    var_w = float(log_returns.var())

    annual_return = (math.exp(mean_w * 52) - 1) * 100
    annual_vol = math.sqrt(max(var_w, 0.0) * 52) * 100

    return {
        "ret": round(annual_return, 2),
        "vol": round(annual_vol, 2),
        "points": int(len(closes)),
    }


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
        metrics = compute_metrics(symbol)
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
        info = {}
        try:
            info = yf.Ticker(symbol).fast_info
        except Exception:
            pass
        metrics = compute_metrics(symbol)
        currency = None
        try:
            currency = info.get("currency") if hasattr(info, "get") else getattr(info, "currency", None)
        except Exception:
            currency = None
        result = {"symbol": symbol, "currency": currency, "error": None, **metrics}
    except Exception as exc:
        result = {"symbol": symbol, "error": str(exc)}
    _cache[cache_key] = (now, result)
    return result
