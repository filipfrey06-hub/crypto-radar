"""
Fetcher: Birdeye — holder data, whale movements, token security.

Free tier (public API, bez klucza API dla podstawowych endpointów):
  https://public-api.birdeye.so

Klucz API (opcjonalny, do bardziej zaawansowanych endpointów) może być
ustawiony jako BIRDEYE_API_KEY w .env — na razie używamy publicznych endpointów.
"""
import logging
import os
import requests
from typing import Optional

from .models import TokenCandidate
from ..config import cfg

log = logging.getLogger(__name__)

BIRDEYE_BASE = cfg["birdeye"]["base_url"]
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY", "")  # opcjonalny


def _headers() -> dict:
    h = {"Accept": "application/json", "x-chain": "solana"}
    if BIRDEYE_API_KEY:
        h["X-API-KEY"] = BIRDEYE_API_KEY
    return h


def _get(endpoint: str, params: dict | None = None, timeout: int = 15) -> dict | None:
    try:
        r = requests.get(
            f"{BIRDEYE_BASE}{endpoint}",
            params=params,
            headers=_headers(),
            timeout=timeout,
        )
        r.raise_for_status()
        data = r.json()
        return data.get("data", data)
    except requests.RequestException as e:
        log.warning(f"Birdeye request failed: {e} | endpoint={endpoint}")
        return None


def get_token_security(mint_address: str) -> dict:
    """
    Pobierz dane bezpieczeństwa tokenu — mint/freeze authority, LP lock.
    Kluczowe dla safety filtra.

    Zwraca dict z polami:
    - mint_disabled: bool
    - freeze_disabled: bool
    - lp_locked: bool
    - lp_lock_pct: float
    - top10_holder_pct: float
    """
    data = _get(f"/defi/token_security", params={"address": mint_address})

    result = {
        "mint_disabled": None,
        "freeze_disabled": None,
        "lp_locked": None,
        "lp_lock_pct": 0.0,
        "top10_holder_pct": 0.0,
    }

    if not data:
        return result

    # Birdeye security response format
    result["mint_disabled"] = not bool(data.get("mintable", True))
    result["freeze_disabled"] = not bool(data.get("freezeable", True))

    # LP lock info
    lp_locked_pct = float(data.get("lpLockPct", data.get("lockedPercent", 0)) or 0)
    result["lp_locked"] = lp_locked_pct >= 90.0  # 90%+ = effectively locked
    result["lp_lock_pct"] = lp_locked_pct

    # Top holders
    top10_pct = float(data.get("top10HolderPercent", data.get("ownerPercentage", 0)) or 0)
    result["top10_holder_pct"] = top10_pct * 100 if top10_pct <= 1 else top10_pct  # normalizuj

    return result


def get_token_overview(mint_address: str) -> Optional[dict]:
    """
    Ogólne dane o tokenie: cena, volume, holder count.
    """
    return _get("/defi/token_overview", params={"address": mint_address})


def get_holders(mint_address: str, limit: int = 20) -> list[dict]:
    """
    Pobierz top holderów tokenu.
    """
    data = _get("/v1/token/holder", params={"address": mint_address, "limit": limit})
    if not data:
        return []
    return data.get("items", []) if isinstance(data, dict) else []


def get_whale_transactions(mint_address: str, min_usd: float | None = None) -> list[dict]:
    """
    Pobierz duże transakcje (whale moves) dla tokenu.
    min_usd — filtruj transakcje poniżej tej wartości.
    """
    min_usd = min_usd or cfg["birdeye"]["whale_threshold_usd"]
    data = _get(
        "/defi/txs/token",
        params={"address": mint_address, "tx_type": "swap", "sort_type": "desc", "limit": 50}
    )
    if not data:
        return []

    txs = data.get("items", []) if isinstance(data, dict) else []
    whale_txs = [
        tx for tx in txs
        if float(tx.get("volumeInUSD", tx.get("value", 0)) or 0) >= min_usd
    ]
    return whale_txs


def enrich_candidate(candidate: TokenCandidate) -> TokenCandidate:
    """
    Uzupełnij TokenCandidate o dane z Birdeye.
    Używane po wstępnym fetchu z DexScreener/Pump.fun.
    """
    security = get_token_security(candidate.address)
    if security:
        candidate.mint_disabled = security["mint_disabled"]
        candidate.freeze_disabled = security["freeze_disabled"]
        candidate.lp_locked = security["lp_locked"]
        candidate.lp_lock_pct = security["lp_lock_pct"]
        if security["top10_holder_pct"] > 0:
            candidate.top10_holder_pct = security["top10_holder_pct"]

    overview = get_token_overview(candidate.address)
    if overview:
        holder_count = int(overview.get("holder", overview.get("holderCount", 0)) or 0)
        if holder_count > 0:
            candidate.holder_count = holder_count
        if candidate.volume_24h == 0:
            candidate.volume_24h = float(overview.get("v24hUSD", overview.get("volume24h", 0)) or 0)
        if candidate.market_cap_usd == 0:
            candidate.market_cap_usd = float(overview.get("mc", overview.get("marketCap", 0)) or 0)

    return candidate


def get_new_listings(limit: int = 50) -> list[dict]:
    """
    Pobierz nowe tokeny z Birdeye (alternatywa dla DexScreener).
    """
    data = _get("/defi/tokenlist", params={
        "sort_by": "v24hChangePercent",
        "sort_type": "desc",
        "offset": 0,
        "limit": limit,
        "min_liquidity": cfg["dexscreener"]["min_liquidity_usd"],
    })
    if not data:
        return []
    return data.get("tokens", []) if isinstance(data, dict) else []
