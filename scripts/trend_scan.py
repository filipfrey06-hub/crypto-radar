"""
Trend Scan — co 6 godzin.

Co robi:
1. Pobiera makro dane (BTC dominance, market cap, altseason proxy)
2. Liczy wzmianki narracyjne w RSS (AI, DePIN, RWA, Meme, Neobank...)
3. Porównuje z baseline z ostatnich 7 dni → wykrywa spike'i
4. Dla każdego wykrytego trendu szuka pasujących tokenów na DexScreener
5. Wysyła alerty: trend + tokeny + makro sygnały

Uruchamiany przez GitHub Actions: .github/workflows/trend-scan.yml
"""
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import cfg
from src.state import (
    init_db, cleanup_expired,
    save_narrative_counts, get_narrative_baseline,
    save_btc_dominance, get_prev_btc_dominance,
    was_trend_alerted, mark_trend_alerted,
    was_social_seen, mark_social_seen,
    cleanup_old_trend_data, start_scan_run, finish_scan_run,
)
from src.fetchers.trend_detector import (
    run_trend_scan, NARRATIVE_KEYWORDS,
    fetch_coingecko_trending,
)
from src.fetchers.dexscreener import search_by_keyword
from src.alerts.telegram import (
    send_trend_alert, send_macro_alert, send_vc_funding_alert, send_system_alert,
)

logging.basicConfig(
    level=getattr(logging, cfg["general"]["log_level"], logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("trend_scan")

DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
TREND_BOT_TOKEN = env.TREND_TELEGRAM_BOT_TOKEN or env.TELEGRAM_BOT_TOKEN
TREND_CHAT_ID   = env.TREND_TELEGRAM_CHAT_ID   or env.TELEGRAM_CHAT_ID

# Próg spike'a — ile razy ponad baseline żeby alertować
SPIKE_THRESHOLD = 2.5
# Cooldown między alertami dla tego samego trendu (godziny)
TREND_COOLDOWN_H = 12
# Minimum baseline runs zanim zaczniemy alertować (żeby mieć dane)
MIN_BASELINE_RUNS = 3


def find_matching_tokens(category: str) -> list[dict]:
    """
    Znajdź tokeny Solana pasujące do kategorii narracyjnej.
    Używa DexScreener search po słowach kluczowych.
    """
    keywords = NARRATIVE_KEYWORDS.get(category, [])
    if not keywords:
        return []

    candidates = []
    # Szukaj po pierwszych 3 słowach kluczowych
    for kw in keywords[:3]:
        try:
            results = search_by_keyword(kw, chain="solana")
            for r in results:
                # Podstawowe filtry
                if r.liquidity_usd < 15000:
                    continue
                if r.volume_24h < 5000:
                    continue
                candidates.append({
                    "address":        r.address,
                    "symbol":         r.symbol,
                    "name":           r.name,
                    "market_cap_usd": r.market_cap_usd,
                    "liquidity_usd":  r.liquidity_usd,
                    "volume_24h":     r.volume_24h,
                    "price_change_h24": r.extra.get("price_change_h24", 0),
                    "dex_url":        r.source_url,
                })
            time.sleep(0.5)  # rate limit
        except Exception as e:
            log.debug(f"Token search '{kw}' error: {e}")

    # Deduplikuj po adresie i sortuj po volume
    seen = set()
    unique = []
    for c in candidates:
        if c["address"] not in seen:
            seen.add(c["address"])
            unique.append(c)

    unique.sort(key=lambda x: x["volume_24h"], reverse=True)
    return unique[:5]


def main():
    log.info("╔══════════════════════════════════════╗")
    log.info("║  TREND SCAN — Start                  ║")
    log.info("╚══════════════════════════════════════╝")
    log.info(f"DRY_RUN={DRY_RUN} | Spike threshold: {SPIKE_THRESHOLD}x")

    start = time.time()
    init_db()
    cleanup_expired()
    cleanup_old_trend_data()

    run_id = start_scan_run("trend_scan")
    errors = []
    total_alerts = 0

    # Pobierz poprzedni BTC dominance (do wykrycia zmiany)
    prev_btc_dom = get_prev_btc_dominance(hours_ago=6)

    # Pobierz baseline z 7 dni
    baseline = get_narrative_baseline(days=7)
    has_baseline = len(baseline) >= MIN_BASELINE_RUNS
    log.info(f"Baseline: {len(baseline)} kategorii | has_baseline={has_baseline}")

    # Uruchom pełny trend scan
    try:
        result = run_trend_scan(
            prev_btc_dominance=prev_btc_dom,
            baseline_rss=baseline if has_baseline else None,
        )
    except Exception as e:
        log.error(f"Trend scan błąd: {e}", exc_info=True)
        errors.append(str(e))
        finish_scan_run(run_id, {"errors": errors})
        return

    # Zapisz aktualne dane do baseline (na następny run)
    if result.rss_narrative_counts:
        save_narrative_counts(result.rss_narrative_counts)

    if result.btc_dominance > 0:
        save_btc_dominance(result.btc_dominance)

    log.info(f"BTC dominance: {result.btc_dominance:.1f}% | Market cap: ${result.total_market_cap_b:.0f}B")
    log.info(f"Narrative trends wykrytych: {len(result.narrative_trends)}")
    log.info(f"Makro sygnałów: {len(result.macro_signals)}")
    log.info(f"VC funding alerts: {len(result.vc_alerts)}")

    # ── ALERTY: Trendy narracyjne ──────────────────────────────────────────
    if has_baseline:
        for trend in result.narrative_trends:
            trend_key = f"narrative_{trend.category}"

            if was_trend_alerted(trend_key, cooldown_hours=TREND_COOLDOWN_H):
                log.info(f"Trend {trend.category} w cooldown — pomijam")
                continue

            log.info(f"TREND ALERT: {trend.category} | {trend.strength:.1f}x | score={trend.score}")

            # Znajdź pasujące tokeny
            matching_tokens = find_matching_tokens(trend.category)
            log.info(f"  Znaleziono {len(matching_tokens)} pasujących tokenów")

            if not DRY_RUN:
                ok = send_trend_alert(
                    category       = trend.category,
                    strength       = trend.strength,
                    score          = trend.score,
                    evidence       = trend.evidence,
                    is_new         = trend.is_new,
                    matching_tokens= matching_tokens,
                    macro_signals  = [m.description for m in result.macro_signals],
                    bot_token      = TREND_BOT_TOKEN,
                    chat_id        = TREND_CHAT_ID,
                )
                if ok:
                    mark_trend_alerted(trend_key)
                    total_alerts += 1
            else:
                log.info(f"[DRY RUN] Trend alert: {trend.category} | tokeny: {[t['symbol'] for t in matching_tokens]}")
    else:
        log.info(f"Za mało danych baseline ({len(baseline)}/{MIN_BASELINE_RUNS} runs) — jeszcze nie alertuję, buduję historię")

    # ── ALERTY: Makro sygnały ──────────────────────────────────────────────
    for signal in result.macro_signals:
        signal_key = f"macro_{signal.signal_type}"

        if was_trend_alerted(signal_key, cooldown_hours=24):  # 24h cooldown dla makro
            log.info(f"Makro {signal.signal_type} w cooldown")
            continue

        log.info(f"MAKRO ALERT: {signal.signal_type} — {signal.description}")

        if not DRY_RUN:
            ok = send_macro_alert(
                signal_type = signal.signal_type,
                description = signal.description,
                is_bullish  = signal.is_bullish,
                bot_token   = TREND_BOT_TOKEN,
                chat_id     = TREND_CHAT_ID,
            )
            if ok:
                mark_trend_alerted(signal_key)
                total_alerts += 1
        else:
            log.info(f"[DRY RUN] Makro alert: {signal.signal_type}")

    # ── ALERTY: VC Funding ─────────────────────────────────────────────────
    for vc in result.vc_alerts[:3]:  # max 3 VC alerty per run
        if was_social_seen(vc.url):
            continue

        mark_social_seen(vc.url, vc.source, vc.title)
        log.info(f"VC FUNDING: {vc.title[:60]} | {vc.amount_str} | {vc.category}")

        if not DRY_RUN:
            ok = send_vc_funding_alert(
                title     = vc.title,
                url       = vc.url,
                source    = vc.source,
                amount    = vc.amount_str,
                category  = vc.category,
                bot_token = TREND_BOT_TOKEN,
                chat_id   = TREND_CHAT_ID,
            )
            if ok:
                total_alerts += 1
        else:
            log.info(f"[DRY RUN] VC alert: {vc.title[:60]}")

    elapsed = time.time() - start
    finish_scan_run(run_id, {
        "total_candidates": len(result.narrative_trends),
        "alerts_sent": total_alerts,
        "errors": errors,
    })

    log.info(f"╔══════════════════════════════════════╗")
    log.info(f"║  TREND SCAN — Koniec ({elapsed:.1f}s)")
    log.info(f"║  Alerty: {total_alerts} | Trendy: {len(result.narrative_trends)}")
    log.info(f"╚══════════════════════════════════════╝")


if __name__ == "__main__":
    main()
