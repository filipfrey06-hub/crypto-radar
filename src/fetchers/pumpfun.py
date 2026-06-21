"""
Fetcher: Pump.fun — nowe launches, dev sell detection, bonding curve progress.

Pump.fun API (nieoficjalne, ale publiczne):
  https://frontend-api.pump.fun/coins?sort=creation_time&order=DESC&limit=50
Photon API (agregator pump.fun analytics):
  https://api.photon-sol.tinyastro.io/

Brak klucza dla podstawowych endpointów.
"""
import logging
import requests
from datetime import datetime, timezone
from typing import Optional

from .models import TokenCandidate
from ..config import cfg

log = logging.getLogger(__name__)

PUMP_API = "https://frontend-api-v3.pump.fun"
PUMP_API_V2 = "https://frontend-api-v2.pump.fun"
HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Origin": "https://pump.fun",
    "Referer": "https://pump.fun/",
}


def _get(url: str, params: dict | None = None, timeout: int = 15) -> dict | list | None:
    """Próbuje główne URL, potem fallback na v2."""
    for base_url in [url, url.replace(PUMP_API, PUMP_API_V2)]:
        try:
            r = requests.get(base_url, params=params, headers=HEADERS, timeout=timeout)
            if r.status_code == 530:
                log.debug(f"Pump.fun 530 na {base_url}, próbuję fallback...")
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            log.debug(f"Pump.fun request failed: {e} | url={base_url}")
            continue
    log.warning(f"Pump.fun niedostępny (wszystkie endpointy zwróciły błąd)")
    return None


def fetch_new_launches(limit: int = 50, min_market_cap: float = 5000) -> list[TokenCandidate]:
    """
    Pobierz najnowsze tokeny z Pump.fun, posortowane po dacie utworzenia.
    """
    data = _get(
        f"{PUMP_API}/coins",
        params={"sort": "creation_time", "order": "DESC", "limit": limit, "includeNsfw": "false"}
    )
    if not data:
        log.warning("Pump.fun: brak danych dla nowych launches")
        return []

    coins = data if isinstance(data, list) else data.get("coins", [])
    candidates = []

    for coin in coins:
        try:
            c = _parse_pump_coin(coin)
            if c and c.market_cap_usd >= min_market_cap:
                candidates.append(c)
        except Exception as e:
            log.debug(f"Pump.fun parse error: {e}")

    log.info(f"Pump.fun new launches: {len(candidates)} kandydatów")
    return candidates


def fetch_graduating_tokens(min_curve_pct: float = 50.0) -> list[TokenCandidate]:
    """
    Tokeny bliskie ukończenia bonding curve (graduation = listing na Raydium).
    To jest jeden z najważniejszych sygnałów early entry.
    """
    # Pobierz tokeny posortowane po market cap (proxy dla curve progress)
    data = _get(
        f"{PUMP_API}/coins",
        params={"sort": "market_cap", "order": "DESC", "limit": 100, "includeNsfw": "false"}
    )
    if not data:
        return []

    coins = data if isinstance(data, list) else data.get("coins", [])
    candidates = []

    for coin in coins:
        try:
            c = _parse_pump_coin(coin)
            if c and c.bonding_curve_pct >= min_curve_pct and not c.extra.get("complete", False):
                candidates.append(c)
        except Exception as e:
            log.debug(f"Pump.fun graduating parse error: {e}")

    candidates.sort(key=lambda x: x.bonding_curve_pct, reverse=True)
    log.info(f"Pump.fun graduating (curve >= {min_curve_pct}%): {len(candidates)} kandydatów")
    return candidates


def fetch_token_details(mint_address: str) -> Optional[TokenCandidate]:
    """Pobierz szczegóły konkretnego tokenu pump.fun."""
    data = _get(f"{PUMP_API}/coins/{mint_address}")
    if not data:
        return None
    try:
        return _parse_pump_coin(data)
    except Exception as e:
        log.debug(f"Pump.fun token details error: {e}")
        return None


def fetch_trending_pump(limit: int = 20) -> list[TokenCandidate]:
    """Trending tokeny na Pump.fun (posortowane po volume)."""
    data = _get(
        f"{PUMP_API}/coins",
        params={"sort": "last_trade_timestamp", "order": "DESC", "limit": limit}
    )
    if not data:
        return []

    coins = data if isinstance(data, list) else data.get("coins", [])
    candidates = []
    for coin in coins:
        try:
            c = _parse_pump_coin(coin)
            if c:
                candidates.append(c)
        except Exception:
            pass

    return candidates


def _parse_pump_coin(coin: dict) -> Optional[TokenCandidate]:
    """Parsuj dane pump.fun → TokenCandidate."""
    mint = coin.get("mint", "")
    if not mint:
        return None

    # Wiek tokenu
    created_ts = coin.get("created_timestamp", 0)
    age_minutes = 0.0
    if created_ts:
        age_minutes = (datetime.now(timezone.utc).timestamp() * 1000 - created_ts) / 60000

    # Bonding curve progress
    # virtual_sol_reserves i virtual_token_reserves → wylicz % ukończenia
    # Pump.fun graduation = 85 SOL w virtual_sol_reserves (ok. $8.5k przy SOL=$100)
    # Prostsze: używaj pola "complete" i market_cap vs target ($69k = graduation)
    market_cap = float(coin.get("usd_market_cap", 0) or 0)
    graduation_target = 69000  # $69k market cap = graduation na Raydium
    bonding_pct = min(100.0, (market_cap / graduation_target) * 100) if market_cap else 0.0
    complete = bool(coin.get("complete", False))

    # Jeśli już graduated — bonding curve = 100%
    if complete:
        bonding_pct = 100.0

    # Dev sell detection: sprawdź czy creator_token_holding jest 0 lub bardzo małe
    creator_holding = float(coin.get("creator_token_holdings", coin.get("creator_tokens", 0)) or 0)
    total_supply = float(coin.get("total_supply", 1_000_000_000) or 1_000_000_000)
    dev_sold = creator_holding < (total_supply * 0.001)  # dev ma < 0.1% = prawdopodobnie sprzedał

    return TokenCandidate(
        address=mint,
        symbol=coin.get("symbol", "?"),
        name=coin.get("name", coin.get("symbol", "?")),
        chain="solana",
        price_usd=float(coin.get("price", coin.get("sol_price", 0)) or 0),
        market_cap_usd=market_cap,
        volume_24h=float(coin.get("volume_24h", 0) or 0),
        token_age_minutes=age_minutes,
        is_pump_fun=True,
        bonding_curve_pct=bonding_pct,
        source="pump.fun",
        source_url=f"https://pump.fun/{mint}",
        extra={
            "complete": complete,          # czy już graduated
            "dev_sold": dev_sold,          # czy dev sprzedał tokeny
            "creator": coin.get("creator", ""),
            "description": coin.get("description", "")[:200],
            "image_uri": coin.get("image_uri", ""),
            "twitter": coin.get("twitter", ""),
            "website": coin.get("website", ""),
            "telegram": coin.get("telegram", ""),
            "reply_count": coin.get("reply_count", 0),
            "creator_holding_pct": (creator_holding / total_supply * 100) if total_supply else 0,
        }
    )
