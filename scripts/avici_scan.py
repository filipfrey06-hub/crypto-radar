"""
AVICI Scan — co godzinę.

Co robi:
1. Pobiera aktualną cenę/volume $AVICI z DexScreener
2. Analizuje sygnały (price spike, volume spike, buy pressure)
3. Sprawdza propozycje MetaDAO (nowe / rozstrzygnięte)
4. Skanuje RSS pod kątem wzmianek o AVICI/MetaDAO
5. Wysyła alerty do Telegram Avici Poland

Uruchamiany przez GitHub Actions: .github/workflows/avici-scan.yml
"""
import logging
import os
import sys
import time
from pathlib import Path

# Dodaj root projektu do path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import cfg, env
from src.state import (
    init_db, cleanup_expired, cleanup_old_avici_snapshots,
    save_avici_snapshot, get_known_proposal_ids,
    mark_proposal_seen, was_proposal_new_alerted, mark_proposal_new_alerted,
    was_proposal_resolved_alerted, mark_proposal_resolved_alerted,
    get_rpc_proposal_count, was_social_seen, mark_social_seen,
    start_scan_run, finish_scan_run,
)
from src.fetchers.avici_monitor import fetch_avici, fetch_meta, analyze_avici
from src.fetchers.metadao import fetch_metadao_status, get_proposals_url
from src.fetchers.rss_fetcher import fetch_crypto_news
from src.alerts.telegram import (
    send_avici_price_alert, send_metadao_proposal_alert,
    send_avici_social_alert, send_system_alert,
)

# Konfiguracja logowania
logging.basicConfig(
    level=getattr(logging, cfg["general"]["log_level"], logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("avici_scan")

DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
AVICI_BOT_TOKEN = env.AVICI_TELEGRAM_BOT_TOKEN or env.TELEGRAM_BOT_TOKEN  # fallback do głównego bota
AVICI_CHAT_ID   = env.AVICI_TELEGRAM_CHAT_ID   or env.TELEGRAM_CHAT_ID    # fallback do głównego chatu


def _check_avici_cooldown(signal_type: str) -> bool:
    """
    Sprawdź czy minął cooldown dla danego typu sygnału.
    Używa seen_social tabeli jako prostego cooldown store.
    """
    cooldown_key = f"avici_cooldown_{signal_type}"
    cooldown_h = cfg["avici"]["alert_cooldown_h"]
    # Sprawdź w seen_social (reużywamy tabelę dla cooldownów)
    from src.state import _db
    import time
    cutoff = int(time.time()) - cooldown_h * 3600
    with _db() as conn:
        row = conn.execute(
            "SELECT seen_at FROM seen_social WHERE url = ? AND seen_at > ?",
            (cooldown_key, cutoff)
        ).fetchone()
    return row is None  # True = można alertować


def _set_avici_cooldown(signal_type: str):
    """Ustaw cooldown dla sygnału."""
    mark_social_seen(f"avici_cooldown_{signal_type}", "avici_monitor", signal_type)


def scan_avici_price():
    """Krok 1: Pobierz cenę AVICI i wyślij alerty jeśli potrzeba."""
    log.info("═══ AVICI Price Scan ═══")
    snap = fetch_avici()

    if not snap.ok:
        log.warning("AVICI: nie udało się pobrać danych z DexScreener")
        return 0

    # Zapisz snapshot do DB (dla weekly digest + historii)
    save_avici_snapshot(
        price_usd       = snap.price_usd,
        price_change_h1 = snap.price_change_h1,
        price_change_h24= snap.price_change_h24,
        volume_h24      = snap.volume_h24,
        liquidity_usd   = snap.liquidity_usd,
        market_cap_usd  = snap.market_cap_usd,
    )

    # Analizuj sygnały
    alerts = analyze_avici(snap)
    sent = 0

    for alert in alerts:
        # Sprawdź cooldown
        if not _check_avici_cooldown(alert.signal_type):
            log.info(f"AVICI: {alert.signal_type} w cooldown — pomijam alert")
            continue

        log.info(f"AVICI ALERT: [{alert.priority}] {alert.description}")

        if DRY_RUN:
            log.info(f"[DRY RUN] Nie wysyłam alertu: {alert.description}")
            continue

        ok = send_avici_price_alert(
            signal_type   = alert.signal_type,
            priority      = alert.priority,
            description   = alert.description,
            price_usd     = snap.price_usd,
            change_h1     = snap.price_change_h1,
            change_h24    = snap.price_change_h24,
            volume_h24    = snap.volume_h24,
            liquidity_usd = snap.liquidity_usd,
            market_cap_usd= snap.market_cap_usd,
            dex_url       = snap.dex_url,
            avici_chat_id = AVICI_CHAT_ID,
            avici_bot_token = AVICI_BOT_TOKEN,
        )
        if ok:
            _set_avici_cooldown(alert.signal_type)
            sent += 1

    return sent


def scan_metadao_proposals():
    """Krok 2: Sprawdź propozycje MetaDAO."""
    log.info("═══ MetaDAO Proposals Scan ═══")

    if not cfg["metadao"]["alert_new_proposals"]:
        log.info("MetaDAO propozycje: alerty wyłączone w config")
        return 0

    status = fetch_metadao_status()
    if not status.ok:
        log.warning("MetaDAO: nie udało się pobrać danych (API niedostępne)")
        return 0

    log.info(f"MetaDAO: {status.total_count} propozycji | źródło: {status.data_source}")

    # Specjalny case: RPC fallback zwraca tylko licznik
    if status.data_source == "rpc" and status.proposals:
        prop = status.proposals[0]
        if prop.state == "rpc_count":
            old_count = get_rpc_proposal_count()
            new_count = prop.raw.get("count", 0)
            if new_count > old_count and old_count > 0:
                log.info(f"MetaDAO RPC: wykryto nowe propozycje! {old_count} → {new_count}")
                if not DRY_RUN:
                    send_metadao_proposal_alert(
                        proposal_id   = "unknown",
                        title         = f"Wykryto nowe propozycje MetaDAO (łącznie: {new_count})",
                        state         = "new_detected",
                        is_new        = True,
                        url           = get_proposals_url(),
                        avici_chat_id = AVICI_CHAT_ID,
            avici_bot_token = AVICI_BOT_TOKEN,
                    )
            mark_proposal_seen(prop.proposal_id, prop.title, prop.state)
            return 1 if new_count > old_count else 0

    # Normalny case: mamy szczegóły propozycji
    sent = 0
    known_ids = get_known_proposal_ids()

    for proposal in status.proposals:
        # Zapisz propozycję w DB
        mark_proposal_seen(proposal.proposal_id, proposal.title, proposal.state)

        # Nowa propozycja
        if (proposal.proposal_id not in known_ids
                and not was_proposal_new_alerted(proposal.proposal_id)
                and cfg["metadao"]["alert_new_proposals"]):

            log.info(f"MetaDAO NOWA propozycja: {proposal.title[:60]}")
            if not DRY_RUN:
                ok = send_metadao_proposal_alert(
                    proposal_id   = proposal.proposal_id,
                    title         = proposal.title,
                    state         = proposal.state,
                    is_new        = True,
                    pass_price    = proposal.pass_price,
                    fail_price    = proposal.fail_price,
                    url           = proposal.url,
                    avici_chat_id = AVICI_CHAT_ID,
            avici_bot_token = AVICI_BOT_TOKEN,
                )
                if ok:
                    mark_proposal_new_alerted(proposal.proposal_id)
                    sent += 1

        # Rozstrzygnięta propozycja
        resolved_states = {"passed", "failed", "rejected", "cancelled", "resolved"}
        if (proposal.state.lower() in resolved_states
                and was_proposal_new_alerted(proposal.proposal_id)
                and not was_proposal_resolved_alerted(proposal.proposal_id)
                and cfg["metadao"]["alert_resolved_proposals"]):

            log.info(f"MetaDAO propozycja ROZSTRZYGNIĘTA [{proposal.state}]: {proposal.title[:60]}")
            if not DRY_RUN:
                ok = send_metadao_proposal_alert(
                    proposal_id   = proposal.proposal_id,
                    title         = proposal.title,
                    state         = proposal.state,
                    is_new        = False,
                    pass_price    = proposal.pass_price,
                    fail_price    = proposal.fail_price,
                    url           = proposal.url,
                    avici_chat_id = AVICI_CHAT_ID,
            avici_bot_token = AVICI_BOT_TOKEN,
                )
                if ok:
                    mark_proposal_resolved_alerted(proposal.proposal_id)
                    sent += 1

    return sent


def scan_avici_social():
    """Krok 3: Skanuj RSS pod kątem wzmianek o AVICI/MetaDAO."""
    log.info("═══ AVICI Social Scan ═══")

    avici_keywords = [kw.lower() for kw in cfg["avici"]["rss_keywords"]]
    all_news = fetch_crypto_news(lookback_hours=2)  # tylko ostatnie 2h (scan co godzinę)

    sent = 0
    for signal in all_news:
        text = f"{signal.title} {signal.content}".lower()

        # Sprawdź czy zawiera słowa kluczowe AVICI/MetaDAO
        matched_kw = [kw for kw in avici_keywords if kw in text]
        if not matched_kw:
            continue

        # Sprawdź czy już widzieliśmy ten artykuł
        if was_social_seen(signal.url):
            continue

        mark_social_seen(signal.url, signal.source, signal.title)
        log.info(f"AVICI social: '{signal.title[:60]}' | kw: {matched_kw[:2]} | {signal.source}")

        # Nie wysyłaj za dużo — max 3 alerty social per run
        if sent >= 3:
            log.info("AVICI social: osiągnięto limit 3 alertów na run")
            break

        if not DRY_RUN:
            ok = send_avici_social_alert(
                title         = signal.title,
                url           = signal.url,
                source        = signal.source,
                snippet       = signal.content[:300] if signal.content else "",
                avici_chat_id = AVICI_CHAT_ID,
            avici_bot_token = AVICI_BOT_TOKEN,
            )
            if ok:
                sent += 1

    return sent


def main():
    log.info("╔══════════════════════════════════════╗")
    log.info("║  AVICI SCAN — Start                  ║")
    log.info("╚══════════════════════════════════════╝")
    log.info(f"DRY_RUN={DRY_RUN} | Chat: {'AVICI Poland' if env.AVICI_TELEGRAM_CHAT_ID else 'główny'}")

    start = time.time()
    init_db()
    cleanup_expired()
    cleanup_old_avici_snapshots()

    run_id = start_scan_run("avici_scan")
    errors = []
    total_alerts = 0

    # 1. Cena AVICI
    try:
        n = scan_avici_price()
        total_alerts += n
    except Exception as e:
        log.error(f"AVICI price scan błąd: {e}", exc_info=True)
        errors.append(str(e))

    # 2. Propozycje MetaDAO
    try:
        n = scan_metadao_proposals()
        total_alerts += n
    except Exception as e:
        log.error(f"MetaDAO proposals scan błąd: {e}", exc_info=True)
        errors.append(str(e))

    # 3. Social/news
    try:
        n = scan_avici_social()
        total_alerts += n
    except Exception as e:
        log.error(f"AVICI social scan błąd: {e}", exc_info=True)
        errors.append(str(e))

    elapsed = time.time() - start
    finish_scan_run(run_id, {
        "total_candidates": 1,
        "alerts_sent": total_alerts,
        "errors": errors,
    })

    log.info(f"╔══════════════════════════════════════╗")
    log.info(f"║  AVICI SCAN — Koniec ({elapsed:.1f}s)        ")
    log.info(f"║  Wysłano alertów: {total_alerts}")
    log.info(f"╚══════════════════════════════════════╝")

    if errors:
        log.warning(f"Błędy ({len(errors)}): {errors}")


if __name__ == "__main__":
    main()
