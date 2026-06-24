"""
Monitor $AVICI token — cena, volume, whale signals.

Contract: BANKJmvhT8tiJRsBSS1n2HryMBPvT5Ze4HU95DUAmeta
DEX: Meteora DAMM V2 (główna para), Raydium

Używa DexScreener (brak klucza API).
"""
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import requests

log = logging.getLogger(__name__)

AVICI_ADDRESS = "BANKJmvhT8tiJRsBSS1n2HryMBPvT5Ze4HU95DUAmeta"
META_ADDRESS  = "METAwkXcqyXKy1AtsSgJ8JiUHwGCafnZL38n3vYmeta"
DEXSCREENER   = "https://api.dexscreener.com"
HEADERS       = {"Accept": "application/json", "User-Agent": "crypto-radar/2.0"}


@dataclass
class AviciSnapshot:
    """Aktualny stan tokenu $AVICI."""
    price_usd: float = 0.0
    price_change_h1: float = 0.0
    price_change_h24: float = 0.0
    price_change_h6: float = 0.0
    volume_h1: float = 0.0
    volume_h24: float = 0.0
    liquidity_usd: float = 0.0
    market_cap_usd: float = 0.0
    fdv_usd: float = 0.0
    txns_h1_buys: int = 0
    txns_h1_sells: int = 0
    txns_h24_buys: int = 0
    txns_h24_sells: int = 0
    dex_url: str = ""
    pair_address: str = ""
    dex_id: str = ""
    ok: bool = False  # czy fetch się udał


@dataclass
class MetaSnapshot:
    """Aktualny stan tokenu $META (MetaDAO governance token)."""
    price_usd: float = 0.0
    price_change_h24: float = 0.0
    volume_h24: float = 0.0
    liquidity_usd: float = 0.0
    market_cap_usd: float = 0.0
    ok: bool = False


@dataclass
class AviciAlert:
    """Pojedynczy sygnał alertu dla AVICI."""
    signal_type: str      # PRICE_SPIKE | PRICE_DROP | VOLUME_SPIKE | WHALE_BUY | WHALE_SELL
    priority: str         # HIGH | MED | LOW
    description: str
    snapshot: AviciSnapshot = field(default_factory=AviciSnapshot)


def _get(url: str, timeout: int = 10) -> Optional[dict | list]:
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        log.warning(f"DexScreener AVICI fetch error: {e}")
        return None


def fetch_avici() -> AviciSnapshot:
    """
    Pobierz aktualny stan $AVICI z DexScreener.
    Zwraca najlepszą parę (największa płynność).
    """
    data = _get(f"{DEXSCREENER}/tokens/v1/solana/{AVICI_ADDRESS}")
    if not data:
        log.warning("AVICI: brak danych z DexScreener")
        return AviciSnapshot(ok=False)

    pairs = data if isinstance(data, list) else [data]
    if not pairs:
        return AviciSnapshot(ok=False)

    # Wybierz parę z największą płynnością
    best = max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))

    liq      = best.get("liquidity", {})
    volume   = best.get("volume", {})
    pc       = best.get("priceChange", {})
    txns     = best.get("txns", {})

    snap = AviciSnapshot(
        price_usd         = float(best.get("priceUsd", 0) or 0),
        price_change_h1   = float(pc.get("h1") or 0),
        price_change_h6   = float(pc.get("h6") or 0),
        price_change_h24  = float(pc.get("h24") or 0),
        volume_h1         = float(volume.get("h1") or 0),
        volume_h24        = float(volume.get("h24") or 0),
        liquidity_usd     = float(liq.get("usd") or 0),
        market_cap_usd    = float(best.get("marketCap") or 0),
        fdv_usd           = float(best.get("fdv") or 0),
        txns_h1_buys      = int(txns.get("h1", {}).get("buys") or 0),
        txns_h1_sells     = int(txns.get("h1", {}).get("sells") or 0),
        txns_h24_buys     = int(txns.get("h24", {}).get("buys") or 0),
        txns_h24_sells    = int(txns.get("h24", {}).get("sells") or 0),
        dex_url           = best.get("url", f"https://dexscreener.com/solana/{AVICI_ADDRESS}"),
        pair_address      = best.get("pairAddress", ""),
        dex_id            = best.get("dexId", ""),
        ok                = True,
    )

    log.info(
        f"AVICI: ${snap.price_usd:.6f} | "
        f"Δh1={snap.price_change_h1:+.1f}% | "
        f"Δh24={snap.price_change_h24:+.1f}% | "
        f"Liq=${snap.liquidity_usd:,.0f} | "
        f"Vol24=${snap.volume_h24:,.0f}"
    )
    return snap


def fetch_meta() -> MetaSnapshot:
    """Pobierz aktualny stan $META (MetaDAO governance token)."""
    data = _get(f"{DEXSCREENER}/tokens/v1/solana/{META_ADDRESS}")
    if not data:
        return MetaSnapshot(ok=False)

    pairs = data if isinstance(data, list) else [data]
    if not pairs:
        return MetaSnapshot(ok=False)

    best = max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
    liq = best.get("liquidity", {})
    pc  = best.get("priceChange", {})

    snap = MetaSnapshot(
        price_usd        = float(best.get("priceUsd", 0) or 0),
        price_change_h24 = float(pc.get("h24") or 0),
        volume_h24       = float(best.get("volume", {}).get("h24") or 0),
        liquidity_usd    = float(liq.get("usd") or 0),
        market_cap_usd   = float(best.get("marketCap") or 0),
        ok               = True,
    )
    log.info(f"META: ${snap.price_usd:.4f} | Δ24h={snap.price_change_h24:+.1f}%")
    return snap


def analyze_avici(snap: AviciSnapshot, last_price: float = 0.0) -> list[AviciAlert]:
    """
    Analizuj snapshot AVICI i zwróć listę alertów jeśli wykryto sygnały.

    Triggery:
    - Cena wzrosła/spadła > 15% w 1h → HIGH
    - Cena wzrosła/spadła > 10% w 1h → MED
    - Volume h1 > 3x średniej godzinowej z h24 → MED
    - Pressure kupna > 75% transakcji w h1 (min 10 txns) → MED
    - Cena wzrosła/spadła > 5% w 1h → LOW (tylko logowanie)
    """
    if not snap.ok:
        return []

    alerts: list[AviciAlert] = []
    pc1 = snap.price_change_h1

    # Sygnał 1: Price spike/drop (na podstawie h1)
    if abs(pc1) >= 15:
        direction = "📈 PUMP" if pc1 > 0 else "📉 DUMP"
        alerts.append(AviciAlert(
            signal_type  = "PRICE_SPIKE" if pc1 > 0 else "PRICE_DROP",
            priority     = "HIGH",
            description  = f"{direction} {pc1:+.1f}% w ciągu 1h | Cena: ${snap.price_usd:.6f}",
            snapshot     = snap,
        ))
    elif abs(pc1) >= 10:
        direction = "⬆️ wzrost" if pc1 > 0 else "⬇️ spadek"
        alerts.append(AviciAlert(
            signal_type  = "PRICE_SPIKE" if pc1 > 0 else "PRICE_DROP",
            priority     = "MED",
            description  = f"Znaczny {direction} {pc1:+.1f}% w 1h | Cena: ${snap.price_usd:.6f}",
            snapshot     = snap,
        ))
    elif abs(pc1) >= 5:
        log.info(f"AVICI: zmiana ceny {pc1:+.1f}% w 1h — poniżej progu alertu (10%)")

    # Sygnał 2: Volume spike (h1 > 3x średniej godzinowej)
    if snap.volume_h24 > 0 and snap.volume_h1 > 0:
        avg_h = snap.volume_h24 / 24
        ratio = snap.volume_h1 / avg_h if avg_h > 0 else 0
        if ratio >= 3.0:
            alerts.append(AviciAlert(
                signal_type  = "VOLUME_SPIKE",
                priority     = "MED",
                description  = f"Volume spike: h1 ${snap.volume_h1:,.0f} = {ratio:.1f}× średniej ({avg_h:,.0f}/h)",
                snapshot     = snap,
            ))
            log.info(f"AVICI Volume spike: {ratio:.1f}× (h1=${snap.volume_h1:,.0f})")

    # Sygnał 3: Buy pressure > 75% txns w h1
    total_h1 = snap.txns_h1_buys + snap.txns_h1_sells
    if total_h1 >= 10:
        buy_ratio = snap.txns_h1_buys / total_h1
        if buy_ratio >= 0.75:
            alerts.append(AviciAlert(
                signal_type  = "WHALE_BUY",
                priority     = "MED",
                description  = f"Presja kupna: {buy_ratio:.0%} z {total_h1} transakcji w h1 to buy",
                snapshot     = snap,
            ))
        elif buy_ratio <= 0.25:
            alerts.append(AviciAlert(
                signal_type  = "WHALE_SELL",
                priority     = "MED",
                description  = f"Presja sprzedaży: {1-buy_ratio:.0%} z {total_h1} transakcji w h1 to sell",
                snapshot     = snap,
            ))

    return alerts
