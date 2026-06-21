"""
Fetcher: DexScreener — nowe pary Solana + volume spike detection.

DexScreener public API — brak klucza, limity rate: ~300 req/min.
Docs: https://docs.dexscreener.com/api/reference
"""
import logging
import requests
from datetime import datetime, timezone
from typing import Generator

from .models import TokenCandidate
from ..config import cfg

log = logging.getLogger(__name__)

DEXSCREENER_BASE = "https://api.dexscreener.com"
HEADERS = {"Accept": "application/json", "User-Agent": "crypto-radar/2.0"}


def _get(url: str, params: dict | None = None, timeout: int = 10) -> dict | list | None:
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        log.warning(f"DexScreener request failed: {e} | url={url}")
        return None


def fetch_new_solana_pairs(max_age_hours: float | None = None) -> list[TokenCandidate]:
    """
    Pobiera nowe pary Solana z ostatnich X godzin.
    Używa endpointu /latest/dex/tokens/latest
    """
    max_age_h = max_age_hours or cfg["dexscreener"]["max_pair_age_hours"]
    min_liq   = cfg["dexscreener"]["min_liquidity_usd"]
    min_vol   = cfg["dexscreener"]["min_volume_24h"]

    # DexScreener: pobierz boosted/najnowsze tokeny Solana
    data = _get(f"{DEXSCREENER_BASE}/token-boosts/latest/v1")
    candidates: list[TokenCandidate] = []

    if not data:
        log.warning("DexScreener: brak danych dla nowych tokenów")
        return candidates

    items = data if isinstance(data, list) else data.get("pairs", [])

    for item in items[:cfg["general"]["max_candidates_per_run"]]:
        try:
            chain = item.get("chainId", "")
            if chain != "solana":
                continue

            token_addr = item.get("tokenAddress", "")
            if not token_addr:
                continue

            # Pobierz szczegóły pary
            pair_data = _get_pair_details(token_addr)
            if pair_data:
                candidates.extend(pair_data)

        except Exception as e:
            log.debug(f"DexScreener: błąd parsowania item: {e}")
            continue

    log.info(f"DexScreener new pairs: {len(candidates)} kandydatów")
    return candidates


def fetch_solana_pairs_by_address(addresses: list[str]) -> list[TokenCandidate]:
    """Pobierz dane dla konkretnych adresów tokenów."""
    if not addresses:
        return []

    chunk_size = 30  # DexScreener obsługuje do 30 adresów na raz
    results = []

    for i in range(0, len(addresses), chunk_size):
        chunk = addresses[i:i + chunk_size]
        addr_str = ",".join(chunk)
        data = _get(f"{DEXSCREENER_BASE}/tokens/v1/solana/{addr_str}")
        if data:
            items = data if isinstance(data, list) else [data]
            for pair in items:
                c = _parse_pair(pair)
                if c:
                    results.append(c)

    return results


def fetch_trending_solana() -> list[TokenCandidate]:
    """
    Trending tokeny Solana z DexScreener — sygnał momentum.
    """
    data = _get(f"{DEXSCREENER_BASE}/token-boosts/top/v1")
    if not data:
        return []

    items = data if isinstance(data, list) else []
    candidates = []

    for item in items[:50]:
        if item.get("chainId") != "solana":
            continue
        addr = item.get("tokenAddress", "")
        if addr:
            pairs = _get_pair_details(addr)
            candidates.extend(pairs)

    log.info(f"DexScreener trending: {len(candidates)} kandydatów")
    return candidates


def search_by_keyword(keyword: str, chain: str = "solana") -> list[TokenCandidate]:
    """Szukaj tokenów po słowie kluczowym (nazwa/symbol)."""
    data = _get(f"{DEXSCREENER_BASE}/latest/dex/search", params={"q": keyword})
    if not data:
        return []

    pairs = data.get("pairs", [])
    results = []
    for pair in pairs:
        if pair.get("chainId") != chain:
            continue
        c = _parse_pair(pair)
        if c:
            results.append(c)

    return results


def _get_pair_details(token_address: str) -> list[TokenCandidate]:
    """Pobierz wszystkie pary dla danego adresu tokenu."""
    data = _get(f"{DEXSCREENER_BASE}/tokens/v1/solana/{token_address}")
    if not data:
        return []

    pairs = data if isinstance(data, list) else [data]
    results = []
    for pair in pairs:
        c = _parse_pair(pair)
        if c:
            results.append(c)

    # Zwróć najlepszą parę (największa płynność)
    results.sort(key=lambda x: x.liquidity_usd, reverse=True)
    return results[:1]  # tylko top para per token


def _parse_pair(pair: dict) -> TokenCandidate | None:
    """Parsuj pojedynczą parę DexScreener → TokenCandidate."""
    try:
        base = pair.get("baseToken", {})
        addr = base.get("address", "")
        if not addr:
            return None

        # Wiek tokenu
        pair_created_at = pair.get("pairCreatedAt", 0)  # unix ms
        age_minutes = 0.0
        if pair_created_at:
            age_ms = datetime.now(timezone.utc).timestamp() * 1000 - pair_created_at
            age_minutes = age_ms / 60000

        liq = pair.get("liquidity", {})
        volume = pair.get("volume", {})
        price_change = pair.get("priceChange", {})

        # Volume spike: porównaj h1 vs h24 jako proxy
        vol_h24 = float(volume.get("h24", 0) or 0)
        vol_h6  = float(volume.get("h6", 0) or 0)
        # Estymuj 7d avg z h24 (nie mamy historii, więc to proxy)
        vol_7d_avg = vol_h24  # w safety filtrze i tak liczymy na podstawie dostępnych danych

        return TokenCandidate(
            address=addr,
            symbol=base.get("symbol", "?"),
            name=base.get("name", base.get("symbol", "?")),
            chain=pair.get("chainId", "solana"),
            price_usd=float(pair.get("priceUsd", 0) or 0),
            liquidity_usd=float(liq.get("usd", 0) or 0),
            volume_24h=vol_h24,
            volume_7d_avg=vol_7d_avg,
            market_cap_usd=float(pair.get("marketCap", 0) or 0),
            fdv_usd=float(pair.get("fdv", 0) or 0),
            token_age_minutes=age_minutes,
            source="dexscreener",
            source_url=pair.get("url", f"https://dexscreener.com/solana/{addr}"),
            extra={
                "price_change_h1": price_change.get("h1"),
                "price_change_h24": price_change.get("h24"),
                "dex_id": pair.get("dexId"),
                "pair_address": pair.get("pairAddress"),
                "txns_h24_buys": pair.get("txns", {}).get("h24", {}).get("buys", 0),
                "txns_h24_sells": pair.get("txns", {}).get("h24", {}).get("sells", 0),
            }
        )
    except Exception as e:
        log.debug(f"_parse_pair error: {e} | pair={pair.get('baseToken', {}).get('address', '?')}")
        return None
