"""
Fetcher: RugCheck.xyz — bezpłatny safety check bez klucza API.

Zwraca: mint authority, freeze authority, LP lock, top holders, score.
Docs: https://api.rugcheck.xyz/swagger/index.html

Endpoint: GET https://api.rugcheck.xyz/v1/tokens/{mint}/report
"""
import logging
import requests
from typing import Optional

log = logging.getLogger(__name__)

RUGCHECK_BASE = "https://api.rugcheck.xyz/v1"
HEADERS = {"Accept": "application/json", "User-Agent": "crypto-radar/2.0"}


def _get(url: str, timeout: int = 15) -> Optional[dict]:
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        log.debug(f"RugCheck request failed: {e} | url={url}")
        return None


def get_token_report(mint_address: str) -> dict:
    """
    Pobierz pełny raport bezpieczeństwa tokenu z RugCheck.

    Zwraca dict z polami:
    - mint_disabled: bool
    - freeze_disabled: bool
    - lp_locked: bool
    - lp_lock_pct: float
    - top10_holder_pct: float
    - rugcheck_score: int (0-1000, wyższy = bezpieczniejszy)
    - risks: list[str]  — nazwy wykrytych ryzyk
    - raw: dict         — surowe dane z API
    """
    result = {
        "mint_disabled": None,
        "freeze_disabled": None,
        "lp_locked": None,
        "lp_lock_pct": 0.0,
        "top10_holder_pct": 0.0,
        "rugcheck_score": 0,
        "risks": [],
        "raw": {},
    }

    data = _get(f"{RUGCHECK_BASE}/tokens/{mint_address}/report")
    if not data:
        return result

    result["raw"] = data
    result["rugcheck_score"] = int(data.get("score", 0))

    # Risks
    risks = data.get("risks", [])
    result["risks"] = [r.get("name", "") for r in risks if isinstance(r, dict)]

    # Token info — mint/freeze authority
    token = data.get("token", {})
    if token:
        # null = wyłączone (bezpieczne), string/address = aktywne (niebezpieczne)
        mint_auth = token.get("mintAuthority")
        result["mint_disabled"] = (mint_auth is None or mint_auth == "")

        freeze_auth = token.get("freezeAuthority")
        result["freeze_disabled"] = (freeze_auth is None or freeze_auth == "")

    # Alternatywnie: sprawdź przez risk names
    risk_names = [r.lower() for r in result["risks"]]
    if "mint authority is enabled" in risk_names or "mutable" in " ".join(risk_names):
        result["mint_disabled"] = False
    if "freeze authority is enabled" in risk_names:
        result["freeze_disabled"] = False

    # LP lock — z markets
    markets = data.get("markets", [])
    if markets:
        total_lp_locked = 0.0
        market_count = 0
        for market in markets:
            lp = market.get("lp", {})
            if lp:
                locked_pct = float(lp.get("lpLockedPct", lp.get("lpLockPct", 0)) or 0)
                total_lp_locked += locked_pct
                market_count += 1

        if market_count > 0:
            avg_locked = total_lp_locked / market_count
            result["lp_lock_pct"] = avg_locked
            result["lp_locked"] = avg_locked >= 80.0
        else:
            # Brak markets = pre-DEX (np. pump.fun przed graduation)
            result["lp_locked"] = None
    else:
        result["lp_locked"] = None

    # Sprawdź LP przez risk names
    if "low liquidity" in " ".join(risk_names) or "no liquidity" in " ".join(risk_names):
        result["lp_locked"] = False

    # Top holders
    top_holders = data.get("topHolders", [])
    if top_holders:
        # Suma top 10 (exclude burn/LP addresses)
        burn_addresses = {
            "1nc1nerator11111111111111111111111111111111",
            "burnxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        }
        top10 = [
            h for h in top_holders[:10]
            if h.get("address", "").lower() not in burn_addresses
            and not h.get("isOwner", False)
        ]
        total_pct = sum(float(h.get("pct", 0)) * 100 for h in top10)
        result["top10_holder_pct"] = min(100.0, total_pct)

    log.debug(
        f"RugCheck {mint_address[:8]}: score={result['rugcheck_score']}, "
        f"mint_disabled={result['mint_disabled']}, "
        f"freeze_disabled={result['freeze_disabled']}, "
        f"lp_locked={result['lp_locked']} ({result['lp_lock_pct']:.0f}%), "
        f"top10={result['top10_holder_pct']:.0f}%"
    )

    return result


def batch_check(mint_addresses: list[str]) -> dict[str, dict]:
    """
    Sprawdź wiele tokenów. Zwraca dict {address: report}.
    """
    results = {}
    for addr in mint_addresses:
        results[addr] = get_token_report(addr)
    return results
