"""
AVICI Weekly Digest — poniedziałek 9:00 (Warsaw time = 7:00 UTC).

Co robi:
1. Pobiera aktualną cenę $AVICI i $META
2. Porównuje z ceną sprzed 7 dni (z DB)
3. Zbiera aktywne propozycje MetaDAO
4. Zbiera wiadomości o AVICI/MetaDAO z ostatnich 7 dni
5. Wysyła cotygodniowe podsumowanie do Telegram Avici Poland

Uruchamiany przez GitHub Actions: .github/workflows/avici-digest.yml
"""
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import cfg, env
from src.state import (
    init_db, get_avici_price_7d_ago, get_avici_snapshots_7d,
    get_known_proposal_ids,
)
from src.fetchers.avici_monitor import fetch_avici, fetch_meta
from src.fetchers.metadao import fetch_metadao_status
from src.fetchers.rss_fetcher import fetch_crypto_news
from src.alerts.telegram import send_avici_weekly_digest, send_system_alert

logging.basicConfig(
    level=getattr(logging, cfg["general"]["log_level"], logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("avici_digest")

DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
AVICI_BOT_TOKEN = env.AVICI_TELEGRAM_BOT_TOKEN or env.TELEGRAM_BOT_TOKEN
AVICI_CHAT_ID   = env.AVICI_TELEGRAM_CHAT_ID   or env.TELEGRAM_CHAT_ID


def main():
    log.info("╔══════════════════════════════════════╗")
    log.info("║  AVICI WEEKLY DIGEST — Start         ║")
    log.info("╚══════════════════════════════════════╝")

    init_db()

    # 1. Pobierz dane AVICI i META
    log.info("Pobieranie danych AVICI i META...")
    avici_snap = fetch_avici()
    meta_snap  = fetch_meta()

    if not avici_snap.ok:
        log.error("Nie można pobrać danych AVICI — przerywam digest")
        send_system_alert("AVICI Digest: nie można pobrać danych AVICI z DexScreener", is_error=True)
        return

    # 2. Cena sprzed 7 dni
    price_7d_ago = get_avici_price_7d_ago()
    if price_7d_ago:
        log.info(f"Cena AVICI 7d temu: ${price_7d_ago:.6f}")
    else:
        log.info("Brak historycznych danych ceny (pierwsze uruchomienie lub DB pusta)")

    # 3. Aktywne propozycje MetaDAO
    log.info("Pobieranie propozycji MetaDAO...")
    meta_status = fetch_metadao_status()
    active_proposals = []
    if meta_status.ok:
        active_proposals = [
            {"proposal_id": p.proposal_id, "title": p.title or p.proposal_id}
            for p in meta_status.proposals
            if p.state.lower() in ("active", "pending", "open", "voting", "rpc_count")
        ]
        log.info(f"MetaDAO: {len(active_proposals)} aktywnych propozycji")
    else:
        log.warning("MetaDAO: dane niedostępne")
        active_proposals = [{"proposal_id": "unavailable", "title": "Sprawdź ręcznie: futarchy.metadao.fi/proposals"}]

    # 4. Wiadomości o AVICI/MetaDAO z ostatnich 7 dni
    log.info("Skanowanie newsów AVICI/MetaDAO z ostatnich 7 dni...")
    avici_keywords = [kw.lower() for kw in cfg["avici"]["rss_keywords"]]
    all_news = fetch_crypto_news(lookback_hours=168)  # 7 dni
    news_items = []
    for sig in all_news:
        text = f"{sig.title} {sig.content}".lower()
        if any(kw in text for kw in avici_keywords):
            news_items.append(sig.title)

    log.info(f"Znalezionych wzmianek AVICI/MetaDAO: {len(news_items)}")

    # 5. Wyślij digest
    log.info("Wysyłanie tygodniowego raportu...")
    if DRY_RUN:
        log.info(f"[DRY RUN] Digest:\n"
                 f"  AVICI: ${avici_snap.price_usd:.6f} (7d: {price_7d_ago})\n"
                 f"  META: ${meta_snap.price_usd:.4f}\n"
                 f"  Propozycje: {active_proposals[:3]}\n"
                 f"  Newsy: {news_items[:3]}")
        return

    ok = send_avici_weekly_digest(
        price_now       = avici_snap.price_usd,
        price_7d_ago    = price_7d_ago,
        change_24h      = avici_snap.price_change_h24,
        volume_24h      = avici_snap.volume_h24,
        liquidity_usd   = avici_snap.liquidity_usd,
        market_cap_usd  = avici_snap.market_cap_usd,
        meta_price      = meta_snap.price_usd if meta_snap.ok else 0.0,
        meta_change_24h = meta_snap.price_change_h24 if meta_snap.ok else 0.0,
        active_proposals= active_proposals,
        news_items      = news_items,
        avici_chat_id   = AVICI_CHAT_ID,
        avici_bot_token = AVICI_BOT_TOKEN,
    )

    if ok:
        log.info("✅ Tygodniowy raport AVICI wysłany pomyślnie")
    else:
        log.error("❌ Błąd wysyłania tygodniowego raportu AVICI")


if __name__ == "__main__":
    main()
