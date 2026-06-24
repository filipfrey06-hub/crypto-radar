"""
Trend Detector — wczesne wykrywanie narracji i makro sygnałów.

Źródła:
1. Pump.fun — liczba launchów per kategoria (spike = nowa narracja)
2. DexScreener trending — które kategorie dominują wśród top tokenów
3. CoinGecko Global — BTC dominance, total market cap, altseason proxy
4. RSS narrative frequency — ile razy słowo kluczowe pojawia się w newsach
5. VC/funding newsy — nowe projekty z funding przez RSS

Logika:
- Każda kategoria ma baseline (średnia z ostatnich 7 dni zapisana w DB)
- Spike = aktualne > 2.5x baseline
- Altseason = BTC dominance < 50% AND spada
"""
import logging
import time
from dataclasses import dataclass, field
from typing import Optional
from collections import defaultdict

import requests

log = logging.getLogger(__name__)

HEADERS = {"Accept": "application/json", "User-Agent": "crypto-radar/2.0"}

# Kategorie narracyjne i ich słowa kluczowe
NARRATIVE_KEYWORDS: dict[str, list[str]] = {
    "AI_Agents":   ["ai agent", "artificial intelligence", "llm", "gpt", "neural", "agi", "machine learning", "ai crypto"],
    "DePIN":       ["depin", "physical infrastructure", "hotspot", "bandwidth", "gpu network", "wireless", "node network"],
    "RWA":         ["real world asset", "rwa", "tokenized", "treasury", "real estate", "commodity token"],
    "Gaming":      ["gamefi", "play to earn", "p2e", "metaverse", "nft game", "web3 game", "gaming"],
    "Meme":        ["meme", "pepe", "wojak", "chad", "giga", "doge", "cat coin", "dog coin"],
    "Neobank":     ["neobank", "crypto card", "debit card", "bank", "payment", "spending", "visa", "mastercard"],
    "DeSci":       ["desci", "science", "research", "biotech", "pharma", "longevity"],
    "SocialFi":    ["socialfi", "social", "creator", "content", "influencer", "fan token"],
    "Political":   ["trump", "elon", "musk", "congress", "sec", "regulation", "etf", "strategic reserve"],
}

VC_KEYWORDS = ["raised", "funding", "series a", "seed round", "investment", "backed by", "venture", "million funding", "raises $"]


@dataclass
class NarrativeTrend:
    """Wykryty trend narracyjny."""
    category: str
    strength: float           # ile razy ponad baseline (np. 3.2x)
    signal_sources: list[str] # skąd sygnał: ["pump_fun", "rss", "dexscreener"]
    evidence: list[str]       # konkretne dowody (tytuły artykułów, liczby)
    score: int                # 1-10 siły trendu
    is_new: bool = False      # czy nowa narracja (nie było jej wcześniej)


@dataclass
class MacroSignal:
    """Makro sygnał rynkowy."""
    signal_type: str          # ALTSEASON | BTC_DOMINANCE_DROP | MARKET_CAP_SURGE | FUNDING_SPIKE
    description: str
    value: float              # aktualna wartość
    threshold: float          # próg który przekroczono
    is_bullish: bool = True


@dataclass
class VCFundingAlert:
    """Ogłoszenie o funding nowego projektu."""
    title: str
    url: str
    source: str
    amount_str: str           # np. "$5M", "undisclosed"
    category: str             # AI, DePIN, etc.
    snippet: str = ""


@dataclass
class TrendScanResult:
    """Pełny wynik trend scan."""
    narrative_trends: list[NarrativeTrend] = field(default_factory=list)
    macro_signals: list[MacroSignal] = field(default_factory=list)
    vc_alerts: list[VCFundingAlert] = field(default_factory=list)
    rss_narrative_counts: dict[str, int] = field(default_factory=dict)
    pump_fun_counts: dict[str, int] = field(default_factory=dict)
    btc_dominance: float = 0.0
    total_market_cap_b: float = 0.0
    fetched_at: int = field(default_factory=lambda: int(time.time()))


# ── CoinGecko Global Data ─────────────────────────────────────────────────────

def fetch_macro_data() -> dict:
    """
    Pobierz globalne dane rynkowe z CoinGecko (free, bez klucza).
    Zwraca: btc_dominance, total_market_cap, market_cap_change_24h
    """
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/global",
            headers=HEADERS, timeout=10
        )
        r.raise_for_status()
        data = r.json().get("data", {})
        mc = data.get("total_market_cap", {})
        mc_change = data.get("market_cap_change_percentage_24h_usd", 0)
        btc_dom = data.get("market_cap_percentage", {}).get("btc", 0)
        total_mc_usd = mc.get("usd", 0)

        log.info(f"Makro: BTC dominance={btc_dom:.1f}% | Total MC=${total_mc_usd/1e9:.0f}B | MC change 24h={mc_change:+.1f}%")
        return {
            "btc_dominance": float(btc_dom),
            "total_market_cap_usd": float(total_mc_usd),
            "market_cap_change_24h": float(mc_change),
            "active_cryptos": int(data.get("active_cryptocurrencies", 0)),
        }
    except Exception as e:
        log.warning(f"CoinGecko global data error: {e}")
        return {}


def fetch_coingecko_trending() -> list[dict]:
    """
    Pobierz trending coins z CoinGecko (top 7 wyszukiwań).
    Zwraca listę {name, symbol, market_cap_rank, price_change_24h}
    """
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/search/trending",
            headers=HEADERS, timeout=10
        )
        r.raise_for_status()
        coins = r.json().get("coins", [])
        result = []
        for c in coins:
            item = c.get("item", {})
            result.append({
                "name": item.get("name", ""),
                "symbol": item.get("symbol", ""),
                "market_cap_rank": item.get("market_cap_rank", 999),
                "price_change_24h": item.get("data", {}).get("price_change_percentage_24h", {}).get("usd", 0),
                "sparkline": item.get("data", {}).get("sparkline", ""),
            })
        log.info(f"CoinGecko trending: {[c['symbol'] for c in result]}")
        return result
    except Exception as e:
        log.warning(f"CoinGecko trending error: {e}")
        return []


def analyze_macro_signals(macro: dict, prev_btc_dominance: float = 0.0) -> list[MacroSignal]:
    """Analizuj makro dane i wykryj sygnały."""
    signals = []
    btc_dom = macro.get("btc_dominance", 0)
    mc_change = macro.get("market_cap_change_24h", 0)

    # Sygnał 1: BTC dominance < 50% i spada
    if btc_dom > 0 and btc_dom < 50:
        signals.append(MacroSignal(
            signal_type  = "ALTSEASON_TERRITORY",
            description  = f"BTC dominance {btc_dom:.1f}% — poniżej 50%, altcoiny mogą outperformować",
            value        = btc_dom,
            threshold    = 50.0,
            is_bullish   = True,
        ))

    # Sygnał 2: Duży spadek BTC dominance w 24h (proxy: porównaj z poprzednim)
    if prev_btc_dominance > 0 and btc_dom > 0:
        dom_change = btc_dom - prev_btc_dominance
        if dom_change <= -1.5:  # spadek o 1.5pp+ = altseason signal
            signals.append(MacroSignal(
                signal_type  = "BTC_DOMINANCE_DROP",
                description  = f"BTC dominance spada: {prev_btc_dominance:.1f}% → {btc_dom:.1f}% ({dom_change:+.1f}pp) — silny sygnał altseason",
                value        = dom_change,
                threshold    = -1.5,
                is_bullish   = True,
            ))

    # Sygnał 3: Duży wzrost całego market cap
    if mc_change >= 5:
        total_b = macro.get("total_market_cap_usd", 0) / 1e9
        signals.append(MacroSignal(
            signal_type  = "MARKET_CAP_SURGE",
            description  = f"Total market cap wzrósł o {mc_change:.1f}% w 24h — ${total_b:.0f}B",
            value        = mc_change,
            threshold    = 5.0,
            is_bullish   = True,
        ))
    elif mc_change <= -5:
        signals.append(MacroSignal(
            signal_type  = "MARKET_CAP_DROP",
            description  = f"Total market cap spadł o {mc_change:.1f}% w 24h — ostrożność",
            value        = mc_change,
            threshold    = -5.0,
            is_bullish   = False,
        ))

    return signals


# ── Pump.fun narracje ─────────────────────────────────────────────────────────

def fetch_pumpfun_narrative_counts() -> dict[str, int]:
    """
    Policz tokeny z pump.fun per kategoria narracyjna (ostatnie ~100 launchów).
    Używa DexScreener boosted tokens jako proxy dla pump.fun aktywności.
    """
    try:
        r = requests.get(
            "https://api.dexscreener.com/token-boosts/latest/v1",
            headers=HEADERS, timeout=10
        )
        r.raise_for_status()
        items = r.json() if isinstance(r.json(), list) else []
    except Exception as e:
        log.warning(f"DexScreener pump.fun proxy error: {e}")
        return {}

    counts: dict[str, int] = defaultdict(int)

    for item in items:
        if item.get("chainId") != "solana":
            continue
        # Opis i nazwa tokenu
        desc = (item.get("description", "") or "").lower()
        links = item.get("links", [])
        text = desc + " " + " ".join(str(l) for l in links)

        for category, keywords in NARRATIVE_KEYWORDS.items():
            if any(kw in text for kw in keywords):
                counts[category] += 1
                break  # jedna kategoria per token

    log.info(f"Pump.fun narrative counts: {dict(counts)}")
    return dict(counts)


# ── RSS narrative frequency ───────────────────────────────────────────────────

def count_rss_narratives(lookback_hours: int = 24) -> dict[str, int]:
    """
    Policz wzmianki per kategoria narracyjna w RSS feedach.
    Używa istniejącego rss_fetcher.
    """
    try:
        from .rss_fetcher import fetch_crypto_news
        news = fetch_crypto_news(lookback_hours=lookback_hours)
    except Exception as e:
        log.warning(f"RSS fetch for trend analysis error: {e}")
        return {}

    counts: dict[str, int] = defaultdict(int)

    for signal in news:
        text = f"{signal.title} {signal.content}".lower()
        for category, keywords in NARRATIVE_KEYWORDS.items():
            if any(kw in text for kw in keywords):
                counts[category] += 1

    # Policz też VC/funding
    vc_count = sum(
        1 for s in news
        if any(kw in f"{s.title} {s.content}".lower() for kw in VC_KEYWORDS)
    )
    if vc_count > 0:
        counts["VC_Funding"] = vc_count

    log.info(f"RSS narrative counts (last {lookback_hours}h): {dict(counts)}")
    return dict(counts)


def fetch_vc_funding_alerts(lookback_hours: int = 24) -> list[VCFundingAlert]:
    """Wyłap artykuły o nowym fundingu z RSS feedów."""
    try:
        from .rss_fetcher import fetch_crypto_news
        news = fetch_crypto_news(lookback_hours=lookback_hours)
    except Exception as e:
        log.warning(f"RSS VC funding fetch error: {e}")
        return []

    alerts = []
    import re
    amount_pattern = re.compile(r'\$[\d,.]+\s*[MBK](?:illion|illion)?', re.IGNORECASE)

    for signal in news:
        text = f"{signal.title} {signal.content}".lower()
        if not any(kw in text for kw in VC_KEYWORDS):
            continue

        # Wyciągnij kwotę
        amounts = amount_pattern.findall(signal.title + " " + signal.content)
        amount_str = amounts[0] if amounts else "undisclosed"

        # Klasyfikuj kategorię
        category = "General"
        for cat, keywords in NARRATIVE_KEYWORDS.items():
            if any(kw in text for kw in keywords):
                category = cat
                break

        alerts.append(VCFundingAlert(
            title      = signal.title,
            url        = signal.url,
            source     = signal.source,
            amount_str = amount_str,
            category   = category,
            snippet    = signal.content[:200] if signal.content else "",
        ))

    log.info(f"VC funding alerts: {len(alerts)} znalezionych")
    return alerts[:10]  # max 10 per run


# ── Główna funkcja ─────────────────────────────────────────────────────────────

def analyze_narrative_trends(
    current_counts: dict[str, int],
    baseline_counts: dict[str, int],
    spike_threshold: float = 2.5,
) -> list[NarrativeTrend]:
    """
    Porównaj aktualne liczby wzmianek z baseline i wykryj spike'i.

    baseline_counts: średnia z poprzednich 7 dni (z state.db)
    spike_threshold: ile razy ponad baseline = trend (default 2.5x)
    """
    trends = []

    all_categories = set(current_counts.keys()) | set(baseline_counts.keys())

    for cat in all_categories:
        current = current_counts.get(cat, 0)
        baseline = baseline_counts.get(cat, 1)  # min 1 żeby uniknąć dzielenia przez 0

        strength = current / baseline
        is_new = baseline <= 1 and current >= 3  # nowa narracja (była marginalna)

        if strength >= spike_threshold or is_new:
            # Oblicz score 1-10
            score = min(10, int(strength * 2))
            if is_new:
                score = max(score, 7)

            evidence = [
                f"Aktualnie: {current} wzmianek",
                f"Baseline (7d avg): {baseline:.0f} wzmianek",
                f"Siła: {strength:.1f}x ponad normę",
            ]

            trends.append(NarrativeTrend(
                category       = cat,
                strength       = strength,
                signal_sources = ["rss"],
                evidence       = evidence,
                score          = score,
                is_new         = is_new,
            ))
            log.info(f"Trend wykryty: {cat} | {strength:.1f}x | score={score}")

    # Sortuj po sile
    trends.sort(key=lambda t: t.score, reverse=True)
    return trends


def run_trend_scan(prev_btc_dominance: float = 0.0,
                   baseline_rss: dict[str, int] | None = None) -> TrendScanResult:
    """Główna funkcja — pełny trend scan."""
    result = TrendScanResult()

    # 1. Makro dane
    macro = fetch_macro_data()
    if macro:
        result.btc_dominance = macro.get("btc_dominance", 0)
        result.total_market_cap_b = macro.get("total_market_cap_usd", 0) / 1e9
        result.macro_signals = analyze_macro_signals(macro, prev_btc_dominance)

    # 2. RSS narrative counts
    result.rss_narrative_counts = count_rss_narratives(lookback_hours=24)

    # 3. Pump.fun narrative proxy
    result.pump_fun_counts = fetch_pumpfun_narrative_counts()

    # 4. VC funding alerts
    result.vc_alerts = fetch_vc_funding_alerts(lookback_hours=24)

    # 5. Analiza trendów (RSS vs baseline)
    if baseline_rss:
        result.narrative_trends = analyze_narrative_trends(
            result.rss_narrative_counts,
            baseline_rss,
        )

    return result
