"""
Backend de données pour ETF PEA Optimizer.

Le calcul des historiques et métriques se fait ici, côté serveur, avec la
librairie `yfinance`. Aucun problème de CORS puisque c'est votre backend
qui contacte Yahoo, pas le navigateur de l'utilisateur.

Ce fichier sert aussi directement la page d'interface (index.html) à la
racine "/" : une fois déployé (ex. sur Render), une seule URL suffit,
utilisable depuis n'importe quel navigateur (y compris Safari iPhone).

Lancement local (sur un ordinateur) :
    pip install -r requirements.txt
    uvicorn app:app --reload --port 8000
puis ouvrez http://localhost:8000 dans un navigateur.

Endpoints :
    GET  /                          -> sert la page d'interface (index.html)
    GET  /api/metrics?isin=...      -> métriques pour un seul ISIN
    POST /api/metrics/batch         -> métriques pour une liste de fonds
                                        body: {"items": [{"key":"wld1","isin":"FR001400U5Q4"},
                                                          {"key":"custom-1","symbol":"CACC.PA"}]}
                                        `key` est renvoyé tel quel dans chaque résultat : c'est
                                        la clé de corrélation utilisée par le frontend (plus fiable
                                        qu'un ISIN, qui peut être absent ou fictif pour un fonds
                                        ajouté manuellement).
    GET  /api/search?q=...          -> recherche de fonds par nom/ISIN (Yahoo)
    GET  /api/metrics-by-symbol     -> métriques pour un ticker Yahoo direct
    GET  /api/history               -> historique de cours (converti en EUR si besoin)

IMPORTANT — ce que Yahoo Finance / yfinance NE fournissent PAS de façon fiable :
l'éligibilité PEA et la disponibilité sur un courtier donné (Trade Republic ou
autre) ne sont pas des données financières générales — l'utilisateur doit les
vérifier lui-même. La politique de distribution est déduite (best-effort,
voir detect_distribution_policy), pas certaine à 100%. Les frais courants
(TER) et l'encours, via `info["annualReportExpenseRatio"]`/`info["totalAssets"]`,
sont eux aussi inconsistants pour les ETF UCITS européens : souvent présents,
parfois absents — chaque champ peut légitimement revenir `None`.
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

app = FastAPI(title="ETF PEA Optimizer — Backend de données", version="1.1")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")


@app.get("/", response_class=HTMLResponse)
def serve_frontend():
    with open(_HTML_PATH, "r", encoding="utf-8") as f:
        return f.read()


# Tickers Yahoo Finance vérifiés manuellement pour certaines parts
# d'accumulation citées dans l'application. Pour tout ISIN absent de cette
# table, le backend tente une résolution automatique via l'API de recherche
# de Yahoo Finance (voir resolve_symbol).
KNOWN_TICKERS: dict[str, str] = {
    "FR001400U5Q4": "DCAM.PA",   # Amundi PEA Monde (MSCI World)
    "IE0002XZSHO1": "WPEA.PA",   # iShares MSCI World Swap PEA
    "FR0011550185": "ESE.PA",    # BNP Paribas Easy S&P 500
    "FR0013412285": "PSP5.PA",   # Amundi PEA S&P 500 Screened
    "FR0011871110": "PUST.PA",   # Amundi PEA Nasdaq-100
    "FR0013412020": "PAEEM.PA",  # Amundi PEA Emerging Markets ESG
    "FR0013380607": "CACC.PA",   # Amundi CAC 40
}

_cache: dict[str, tuple[float, dict]] = {}
CACHE_TTL_SECONDS = 30 * 60  # 30 minutes


class MetricsItem(BaseModel):
    key: str
    isin: Optional[str] = None
    symbol: Optional[str] = None


class MetricsBatchRequest(BaseModel):
    items: list[MetricsItem]


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


# =============================================================================
# Conversion de devises (pour que plusieurs fonds en devises différentes
# soient comparables sur un même graphique — tout est ramené en EUR)
# =============================================================================
def get_fx_series(from_ccy: str, to_ccy: str, start: str, end: str) -> dict:
    """Retourne {date_str: taux} pour convertir 1 unité de from_ccy en
    to_ccy, via le ticker Yahoo Finance 'XXXYYY=X'. Renvoie {} en cas
    d'échec (paire introuvable, réseau, etc.) — géré par l'appelant."""
    if not from_ccy or not to_ccy or from_ccy.upper() == to_ccy.upper():
        return {}
    pair = f"{from_ccy.upper()}{to_ccy.upper()}=X"
    cache_key = f"fx:{pair}:{start}:{end}"
    now = time.time()
    cached = _cache.get(cache_key)
    if cached and (now - cached[0]) < CACHE_TTL_SECONDS:
        return cached[1]
    try:
        start_adj = (pd.Timestamp(start) - pd.Timedelta(days=5)).strftime("%Y-%m-%d")
        end_adj = (pd.Timestamp(end) + pd.Timedelta(days=2)).strftime("%Y-%m-%d")
        hist = yf.Ticker(pair).history(start=start_adj, end=end_adj, interval="1d", auto_adjust=True)
        closes = hist["Close"].dropna()
        result = {d.strftime("%Y-%m-%d"): float(v) for d, v in zip(closes.index, closes.values)}
    except Exception:
        result = {}
    _cache[cache_key] = (now, result)
    return result


def convert_closes_to_eur(dates: list[str], closes: list[float], currency: Optional[str]) -> tuple[list[float], bool, str]:
    """Convertit une série de prix vers l'EUR via le taux de change
    quotidien Yahoo Finance. Échoue silencieusement (renvoie les prix
    bruts inchangés) si le taux est introuvable — l'appelant est informé
    via le booléen et la note renvoyés, à afficher à l'utilisateur plutôt
    que de faire semblant d'avoir converti."""
    if not currency or currency.upper() == "EUR":
        return closes, False, ""
    if not dates:
        return closes, False, ""

    fx_map = get_fx_series(currency, "EUR", dates[0], dates[-1])
    if not fx_map:
        return closes, False, f"conversion {currency}→EUR indisponible pour le moment : prix affichés en {currency} d'origine"

    sorted_fx_dates = sorted(fx_map.keys())
    converted = []
    fx_idx = 0
    last_rate = fx_map[sorted_fx_dates[0]]
    for d, c in zip(dates, closes):
        while fx_idx < len(sorted_fx_dates) and sorted_fx_dates[fx_idx] <= d:
            last_rate = fx_map[sorted_fx_dates[fx_idx]]
            fx_idx += 1
        converted.append(round(c * last_rate, 4) if c is not None else None)
    return converted, True, f"converti de {currency} vers EUR (taux Yahoo Finance {currency}EUR=X)"


# =============================================================================
# Statistiques de performance (rendement annualisé = CAGR, volatilité,
# Sortino, Max Drawdown) sur une fenêtre de cours donnée
# =============================================================================
def window_stats(sub: pd.Series) -> dict:
    """Calcule, à partir d'une série de clôtures hebdomadaires :
      - `ret`  : rendement annualisé (méthode géométrique = CAGR)
      - `vol`  : volatilité annualisée
      - `sortino` : ratio de Sortino annualisé (rendement / volatilité
        des seuls rendements négatifs — pénalise seulement le risque
        baissier, contrairement à la volatilité classique)
      - `max_drawdown` : pire perte cumulée depuis un plus haut, sur la
        fenêtre considérée (valeur négative, en %)
    Retourne des None si l'historique est trop court pour être fiable."""
    if len(sub) < 8:
        return {"ret": None, "vol": None, "sortino": None, "max_drawdown": None}
    log_rets = sub.pct_change().dropna().apply(lambda x: math.log1p(x))
    if log_rets.empty:
        return {"ret": None, "vol": None, "sortino": None, "max_drawdown": None}

    mean_w = float(log_rets.mean())
    var_w = float(log_rets.var())
    ann_ret = (math.exp(mean_w * 52) - 1) * 100
    ann_vol = math.sqrt(max(var_w, 0.0) * 52) * 100

    downside = log_rets[log_rets < 0]
    if len(downside) > 0:
        downside_var = float((downside ** 2).mean())
        downside_dev_annual = math.sqrt(downside_var * 52) * 100
    else:
        downside_dev_annual = 0.0
    sortino = round(ann_ret / downside_dev_annual, 2) if downside_dev_annual > 1e-6 else None

    running_max = sub.cummax()
    drawdown = (sub - running_max) / running_max
    max_dd = round(float(drawdown.min()) * 100, 2) if len(drawdown) else None

    return {
        "ret": round(ann_ret, 2),
        "vol": round(ann_vol, 2),
        "sortino": sortino,
        "max_drawdown": max_dd,
    }


# =============================================================================
# Frais, encours, type d'instrument — best-effort via .info (souvent absent
# pour les ETF UCITS européens : gérer l'absence, pas une erreur en soi)
# =============================================================================
def fetch_fund_info(tkr: "yf.Ticker") -> dict:
    info = {}
    try:
        info = tkr.info or {}
    except Exception:
        info = {}

    ter = None
    for key in ("annualReportExpenseRatio", "expenseRatio", "netExpenseRatio", "totalExpenseRatio"):
        val = info.get(key)
        if val is not None:
            try:
                val = float(val)
                # Yahoo renvoie généralement une fraction (0.002 = 0.20%).
                # Hypothèse non garantie à 100% en l'absence de doc officielle :
                # on suppose une fraction si < 1, un pourcentage direct sinon.
                ter = round(val * 100, 3) if val < 1 else round(val, 3)
                break
            except (TypeError, ValueError):
                continue

    fund_size_million = None
    total_assets = info.get("totalAssets")
    if total_assets is not None:
        try:
            fund_size_million = round(float(total_assets) / 1_000_000, 1)
        except (TypeError, ValueError):
            fund_size_million = None

    return {
        "ter": ter,
        "fund_size_million": fund_size_million,
        "quote_type": info.get("quoteType"),
        "fund_name": info.get("longName") or info.get("shortName"),
        # Catégorie Morningstar/Yahoo (souvent une bonne indication de la zone
        # géographique, ex. "Europe Stock", "Diversified Emerging Mkts") —
        # fournie telle quelle, best-effort, à confirmer par l'utilisateur :
        # ce n'est pas un champ garanti ni systématiquement présent.
        "category_hint": info.get("category"),
    }


# =============================================================================
# Politique de distribution (best-effort) — nom du fonds + historique réel
# des dividendes versés
# =============================================================================
def _name_hint(tkr: "yf.Ticker", symbol: str) -> Optional[str]:
    text = symbol.upper()
    try:
        info = tkr.info or {}
        text += " " + str(info.get("longName") or "").upper()
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
    croisant le nom/ticker du fonds et l'historique réel des dividendes
    versés. Limite connue : un fonds distribuant très récemment lancé peut
    n'avoir versé aucun dividende et sembler "capitalisant" à tort si son
    nom ne donne pas d'indice non plus."""
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


# =============================================================================
# Calcul complet des métriques d'un fonds (un seul téléchargement d'historique)
# =============================================================================
def compute_full_metrics(symbol: str) -> dict:
    """À partir d'un seul téléchargement d'historique (le maximum
    disponible, hebdomadaire) :
      - rendement annualisé = CAGR, volatilité, Sortino et Max Drawdown sur
        1 an / 3 ans / 5 ans / depuis le début de l'historique disponible ;
      - le dernier cours de clôture (natif + converti en EUR) ;
      - TER, encours, type d'instrument et catégorie/zone (best-effort,
        souvent absents ou approximatifs pour les fonds UCITS européens) ;
      - une détection best-effort de la politique de distribution."""
    tkr = yf.Ticker(symbol)
    hist = tkr.history(period="max", interval="1wk", auto_adjust=True)
    if hist.empty:
        raise ValueError(f"aucun historique renvoyé par Yahoo Finance pour {symbol}")
    closes = hist["Close"].dropna()
    if len(closes) < 15:
        raise ValueError(f"historique insuffisant pour {symbol} ({len(closes)} points)")

    last_date = closes.index[-1]
    out: dict = {"points": int(len(closes)), "inception_date": closes.index[0].strftime("%Y-%m-%d")}
    for label, years in (("1y", 1), ("3y", 3), ("5y", 5), ("max", None)):
        if years is None:
            sub = closes  # toute la série disponible, depuis la création du fonds
        else:
            cutoff = last_date - pd.Timedelta(days=int(years * 365.25))
            sub = closes[closes.index >= cutoff]
        stats = window_stats(sub)
        out[f"ret_{label}"] = stats["ret"]
        out[f"vol_{label}"] = stats["vol"]
        out[f"sortino_{label}"] = stats["sortino"]
        out[f"dd_{label}"] = stats["max_drawdown"]
    # Alias par défaut (rétro-compatibilité avec le reste de l'app) : 3 ans.
    # Rappel : "rendement annualisé" et "CAGR" désignent ici EXACTEMENT la
    # même quantité mathématique (taux de croissance annuel composé) —
    # ce n'est pas une coïncidence, ret_Xy EST un CAGR.
    out["ret"] = out["ret_3y"]
    out["vol"] = out["vol_3y"]

    # Devise native du fonds (nécessaire pour la conversion EUR du dernier cours).
    currency = None
    try:
        fi = tkr.fast_info
        currency = fi.get("currency") if hasattr(fi, "get") else getattr(fi, "currency", None)
    except Exception:
        currency = None
    out["currency"] = currency

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

    if out["last_price"] is not None and currency and currency.upper() != "EUR":
        converted, ok, _ = convert_closes_to_eur([out["last_price_date"]], [out["last_price"]], currency)
        out["last_price_eur"] = converted[0] if ok else None
    else:
        out["last_price_eur"] = out["last_price"]

    fund_info = fetch_fund_info(tkr)
    out["ter"] = fund_info["ter"]
    out["fund_size_million"] = fund_info["fund_size_million"]
    out["quote_type"] = fund_info["quote_type"]
    out["fund_name"] = fund_info["fund_name"]
    out["category_hint"] = fund_info["category_hint"]

    dist_policy, dist_basis = detect_distribution_policy(tkr, symbol)
    out["dist_policy"] = dist_policy
    out["dist_basis"] = dist_basis
    return out


def fetch_one(key: str, isin: Optional[str] = None, symbol: Optional[str] = None) -> dict:
    """Calcule les métriques d'un fonds identifié par `key` (l'id interne
    utilisé côté frontend — stable même pour un fonds ajouté manuellement
    sans ISIN réel), à partir d'un symbole Yahoo déjà connu OU d'un ISIN à
    résoudre. Utiliser directement `symbol` quand il est déjà connu évite
    une résolution ISIN inutile (et surtout, un échec de résolution sur un
    ISIN fictif pour les fonds ajoutés manuellement — c'est ce qui causait
    le badge "Estimé" incorrect pour ces fonds)."""
    resolved_symbol = symbol or (resolve_symbol(isin) if isin else None)
    if not resolved_symbol:
        return {
            "key": key, "isin": isin, "symbol": None, "ret": None, "vol": None,
            "error": "aucun symbole Yahoo Finance disponible (ni fourni, ni résolu depuis l'ISIN)",
        }

    cache_key = f"item:{resolved_symbol}"
    now = time.time()
    cached = _cache.get(cache_key)
    if cached and (now - cached[0]) < CACHE_TTL_SECONDS:
        result = dict(cached[1])
        result["key"] = key
        return result

    try:
        metrics = compute_full_metrics(resolved_symbol)
        result = {"isin": isin, "symbol": resolved_symbol, "error": None, **metrics}
    except Exception as exc:
        result = {"isin": isin, "symbol": resolved_symbol, "ret": None, "vol": None, "error": str(exc)}

    _cache[cache_key] = (now, result)
    result_with_key = dict(result)
    result_with_key["key"] = key
    return result_with_key


@app.get("/api/health")
def health():
    return {"status": "ok", "message": "Backend ETF PEA Optimizer actif — voir /docs pour la documentation interactive."}


@app.get("/api/metrics")
def metrics_single(isin: str):
    return fetch_one(key=isin, isin=isin)


@app.post("/api/metrics/batch")
def metrics_batch(payload: MetricsBatchRequest):
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {
            pool.submit(fetch_one, item.key, item.isin, item.symbol): item.key
            for item in payload.items
        }
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:
                results.append({"key": futures[future], "error": str(exc)})
    return {"results": results}


@app.get("/api/search")
def search_funds(q: str):
    """Recherche de fonds par nom ou ISIN via l'API de recherche de Yahoo
    Finance. Ne dit RIEN sur l'éligibilité PEA ni la disponibilité sur un
    courtier — à vérifier vous-même."""
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
    return fetch_one(key=symbol, symbol=symbol)


@app.get("/api/history")
def get_history(symbol: str = None, isin: str = None, range: str = "3y", interval: str = "1wk",
                 start: str = None, end: str = None):
    """Historique de cours pour un fonds, TOUJOURS converti en EUR si la
    devise native diffère (nécessaire pour superposer plusieurs fonds sur
    un même graphique avec une unité commune). Accepte soit un ticker
    Yahoo direct (`symbol`), soit un ISIN à résoudre (`isin`).

    Deux modes :
      - `range` (ex. "3y", "max") : fenêtre glissante se terminant aujourd'hui.
      - `start`/`end` (YYYY-MM-DD, l'un ou l'autre optionnel) : plage de
        dates explicite, prioritaire sur `range` si fournie."""
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

    use_dates = bool(start or end)
    cache_key = f"history:{symbol}:{interval}:" + (f"{start or ''}:{end or ''}" if use_dates else range)
    now = time.time()
    cached = _cache.get(cache_key)
    if cached and (now - cached[0]) < CACHE_TTL_SECONDS:
        return cached[1]
    try:
        tkr = yf.Ticker(symbol)
        if use_dates:
            kwargs = {"interval": interval, "auto_adjust": True}
            if start:
                kwargs["start"] = start
            if end:
                kwargs["end"] = (pd.Timestamp(end) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
            hist = tkr.history(**kwargs)
        else:
            hist = tkr.history(period=range, interval=interval, auto_adjust=True)
        closes = hist["Close"].dropna()
        if closes.empty:
            raise ValueError("aucun historique disponible sur cette période")

        dates = [d.strftime("%Y-%m-%d") for d in closes.index]
        raw_closes = [round(float(c), 4) for c in closes.values]

        currency = None
        try:
            fi = tkr.fast_info
            currency = fi.get("currency") if hasattr(fi, "get") else getattr(fi, "currency", None)
        except Exception:
            currency = None

        eur_closes, converted, note = convert_closes_to_eur(dates, raw_closes, currency)

        result = {
            "symbol": symbol,
            "dates": dates,
            "closes": eur_closes,
            "raw_currency": currency,
            "converted_to_eur": converted,
            "conversion_note": note,
            "error": None,
        }
    except Exception as exc:
        result = {"symbol": symbol, "dates": [], "closes": [], "error": str(exc)}
    _cache[cache_key] = (now, result)
    return result
