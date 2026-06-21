"""
Pipeline Orchestrator — łączy wszystkie 5 warstw w jeden flow.

Każdy job (fast_scan, social_scan, whale_scan, narrative_scan, daily_digest)
wywołuje run_pipeline() z odpowiednimi parametrami.

Flow:
  Fetch → Safety Filter → Activity Score → Claude Score → Alert → State
"""
import logging
import sys
from typing import Optional

from .fetchers.models import TokenCandidate, SocialSignal
from .fetchers import dexscreener, pumpfun, birdeye as birdeye_api, reddit_fetcher, trends, rss_fetcher
from .filters.safety import batch_safety_filter, SafetyResult
from .filters.activity import batch_activity_score
from .scorer import batch_score, ScoreResult
from .alerts import telegram, ntfy
from . import state
from .config import cfg

log = logging.getLogger(__name__)


def setup_logging(level: str | None = None):
    """Skonfiguruj logging dla pipeline."""
    lvl = level or cfg["general"]["log_level"]
    logging.basicConfig(
        level=getattr(logging, lvl, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


# ── FAST SCAN ────────────────────────────────────────────────────────────────

def run_fast_scan() -> dict:
    """
    Szybki skan co 30 minut:
    - Nowe tokeny Solana (DexScreener + Pump.fun)
    - Volume spikes

    Fokus: early detection nowych launchy i momentum plays.
    """
    log.info("═══ START: fast_scan ═══")
    run_id = state.start_scan_run("fast_scan")
    stats = {"errors": []}

    try:
        # ── FETCH ──────────────────────────────────────────────────────────
        candidates: list[TokenCandidate] = []

        # DexScreener: nowe pary Solana
        try:
            dex_candidates = dexscreener.fetch_new_solana_pairs()
            candidates.extend(dex_candidates)
            log.info(f"  DexScreener: {len(dex_candidates)} kandydatów")
        except Exception as e:
            log.error(f"DexScreener fetch error: {e}")
            stats["errors"].append(f"dexscreener: {e}")

        # Pump.fun: nowe launches
        try:
            pump_candidates = pumpfun.fetch_new_launches(limit=50)
            candidates.extend(pump_candidates)
            log.info(f"  Pump.fun: {len(pump_candidates)} kandydatów")
        except Exception as e:
            log.error(f"Pump.fun fetch error: {e}")
            stats["errors"].append(f"pumpfun: {e}")

        # Pump.fun: graduating tokens (sweet spot)
        try:
            graduating = pumpfun.fetch_graduating_tokens(
                min_curve_pct=cfg["activity"]["pump_fun_bonding_curve_pct"]
            )
            candidates.extend(graduating)
            log.info(f"  Pump.fun graduating: {len(graduating)} kandydatów")
        except Exception as e:
            log.error(f"Pump.fun graduating error: {e}")
            stats["errors"].append(f"pumpfun_graduating: {e}")

        # DexScreener trending
        try:
            trending = dexscreener.fetch_trending_solana()
            candidates.extend(trending)
            log.info(f"  DexScreener trending: {len(trending)} kandydatów")
        except Exception as e:
            log.warning(f"DexScreener trending error: {e}")

        # Deduplikuj po adresie
        candidates = _deduplicate(candidates)
        stats["total_candidates"] = len(candidates)
        log.info(f"Total po dedup: {len(candidates)} kandydatów")

        # ── PIPELINE ───────────────────────────────────────────────────────
        stats = _run_token_pipeline(candidates, [], run_id, stats)

    except Exception as e:
        log.error(f"fast_scan critical error: {e}", exc_info=True)
        stats["errors"].append(f"critical: {e}")
        telegram.send_system_alert(f"fast_scan error: {e}", is_error=True)
        state.finish_scan_run(run_id, stats, status="failed")
        return stats

    state.finish_scan_run(run_id, stats)
    log.info(f"═══ END: fast_scan | alerts_sent={stats.get('alerts_sent', 0)} ═══")
    return stats


# ── SOCIAL SCAN ─────────────────────────────────────────────────────────────

def run_social_scan() -> dict:
    """
    Social signals co 1 godzinę:
    - Reddit hot posts
    - Google Trends spikes
    - RSS news (z political detection)
    """
    log.info("═══ START: social_scan ═══")
    run_id = state.start_scan_run("social_scan")
    stats = {"errors": [], "total_candidates": 0}

    try:
        social_signals: list[SocialSignal] = []

        # Reddit
        try:
            reddit_signals = reddit_fetcher.fetch_hot_posts()
            social_signals.extend(reddit_signals)
            log.info(f"  Reddit: {len(reddit_signals)} sygnałów")
        except Exception as e:
            log.error(f"Reddit error: {e}")
            stats["errors"].append(f"reddit: {e}")

        # Google Trends
        try:
            trends_signals = trends.fetch_trending_searches()
            social_signals.extend(trends_signals)
            log.info(f"  Google Trends: {len(trends_signals)} spike'ów")
        except Exception as e:
            log.error(f"Google Trends error: {e}")
            stats["errors"].append(f"trends: {e}")

        # RSS / News
        try:
            rss_signals = rss_fetcher.fetch_crypto_news()
            social_signals.extend(rss_signals)
            log.info(f"  RSS: {len(rss_signals)} artykułów")
        except Exception as e:
            log.error(f"RSS error: {e}")
            stats["errors"].append(f"rss: {e}")

        # Political signals → natychmiastowy alert
        political = [s for s in social_signals if s.narrative_category == "Political"]
        alerts_sent = 0
        for sig in political:
            if state.was_social_seen(sig.url):
                continue
            if telegram.send_social_alert(sig):
                ntfy.send_political_push(sig.title, sig.content[:300], sig.url)
                state.mark_social_seen(sig.url, sig.source, sig.title)
                alerts_sent += 1
                log.info(f"  🏛️ Political alert: {sig.title[:60]}")

        # Tickers wspomniane w social → szybki token scan
        tickers_from_social = rss_fetcher.extract_mentioned_tokens(rss_signals)
        tickers_from_social += list(reddit_fetcher.extract_ticker_mentions(
            [s for s in social_signals if "reddit" in s.source]
        ).keys())
        tickers_from_social = list(set(tickers_from_social))[:20]  # max 20

        if tickers_from_social:
            log.info(f"  Tickers z social: {tickers_from_social}")
            # Szukaj tokenów dla tych tickerów
            token_candidates = []
            for ticker in tickers_from_social[:10]:
                try:
                    found = dexscreener.search_by_keyword(ticker)
                    token_candidates.extend(found[:2])  # max 2 per ticker
                except Exception:
                    pass

            if token_candidates:
                token_candidates = _deduplicate(token_candidates)
                stats["total_candidates"] = len(token_candidates)
                stats = _run_token_pipeline(token_candidates, social_signals, run_id, stats)
                alerts_sent += stats.get("alerts_sent", 0)

        stats["alerts_sent"] = alerts_sent
        stats["social_signals"] = len(social_signals)
        state.cleanup_old_social()

    except Exception as e:
        log.error(f"social_scan critical error: {e}", exc_info=True)
        stats["errors"].append(f"critical: {e}")
        telegram.send_system_alert(f"social_scan error: {e}", is_error=True)
        state.finish_scan_run(run_id, stats, status="failed")
        return stats

    state.finish_scan_run(run_id, stats)
    log.info(f"═══ END: social_scan | alerts_sent={stats.get('alerts_sent', 0)} ═══")
    return stats


# ── WHALE SCAN ───────────────────────────────────────────────────────────────

def run_whale_scan() -> dict:
    """
    Whale movements co 1 godzinę:
    - Duże transakcje na znanych tokenach
    - Nowe duże holderów pozycje
    """
    log.info("═══ START: whale_scan ═══")
    run_id = state.start_scan_run("whale_scan")
    stats = {"errors": [], "total_candidates": 0, "alerts_sent": 0}

    try:
        # Pobierz trending tokeny jako bazę do whale check
        candidates: list[TokenCandidate] = []
        try:
            trending = dexscreener.fetch_trending_solana()
            candidates.extend(trending[:20])
        except Exception as e:
            stats["errors"].append(f"trending: {e}")

        # Sprawdź whale transactions dla każdego
        whale_threshold = cfg["birdeye"]["whale_threshold_usd"]
        for candidate in candidates[:15]:  # limit API calls
            try:
                whale_txs = birdeye_api.get_whale_transactions(
                    candidate.address,
                    min_usd=whale_threshold
                )
                if whale_txs:
                    total_inflow = sum(
                        float(tx.get("volumeInUSD", tx.get("value", 0)) or 0)
                        for tx in whale_txs
                        if tx.get("side", tx.get("type", "")) in ["buy", "in"]
                    )
                    candidate.extra["whale_inflow_usd"] = total_inflow
                    candidate.extra["whale_tx_count"] = len(whale_txs)
                    log.debug(f"  {candidate.symbol}: whale inflow ${total_inflow:,.0f} ({len(whale_txs)} txs)")
            except Exception as e:
                log.debug(f"whale check error {candidate.symbol}: {e}")

        stats["total_candidates"] = len(candidates)
        stats = _run_token_pipeline(candidates, [], run_id, stats)

    except Exception as e:
        log.error(f"whale_scan error: {e}", exc_info=True)
        stats["errors"].append(f"critical: {e}")
        state.finish_scan_run(run_id, stats, status="failed")
        return stats

    state.finish_scan_run(run_id, stats)
    log.info(f"═══ END: whale_scan | alerts_sent={stats.get('alerts_sent', 0)} ═══")
    return stats


# ── NARRATIVE SCAN ───────────────────────────────────────────────────────────

def run_narrative_scan() -> dict:
    """
    Szerszy skan narracyjny co 6 godzin:
    - Altcoiny poza Pump.fun
    - Narrative keywords z DexScreener search
    - Macro context (BTC dominance via RSS)
    """
    log.info("═══ START: narrative_scan ═══")
    run_id = state.start_scan_run("narrative_scan")
    stats = {"errors": [], "total_candidates": 0, "alerts_sent": 0}

    try:
        candidates: list[TokenCandidate] = []
        social_signals: list[SocialSignal] = []

        # RSS z szerszym oknem
        try:
            rss_signals = rss_fetcher.fetch_crypto_news(lookback_hours=6)
            social_signals.extend(rss_signals)
        except Exception as e:
            stats["errors"].append(f"rss: {e}")

        # Szukaj tokenów dla każdej narracji
        for narrative_name, keywords in cfg["narratives"]["keywords"].items():
            for kw in keywords[:2]:  # max 2 keywords per narracja
                try:
                    found = dexscreener.search_by_keyword(kw)
                    for c in found[:3]:
                        c.extra["narrative_search_kw"] = kw
                        c.extra["narrative_cat"] = narrative_name
                    candidates.extend(found[:3])
                except Exception as e:
                    log.debug(f"narrative search error {kw}: {e}")

        candidates = _deduplicate(candidates)
        stats["total_candidates"] = len(candidates)

        if candidates:
            stats = _run_token_pipeline(candidates, social_signals, run_id, stats)

    except Exception as e:
        log.error(f"narrative_scan error: {e}", exc_info=True)
        stats["errors"].append(f"critical: {e}")
        state.finish_scan_run(run_id, stats, status="failed")
        return stats

    state.finish_scan_run(run_id, stats)
    log.info(f"═══ END: narrative_scan | alerts_sent={stats.get('alerts_sent', 0)} ═══")
    return stats


# ── DAILY DIGEST ─────────────────────────────────────────────────────────────

def run_daily_digest() -> dict:
    """
    Dzienny digest o 8:00:
    - Statystyki z ostatnich 24h
    - LOW priority sygnały zebrane w ciągu nocy
    """
    log.info("═══ START: daily_digest ═══")
    run_id = state.start_scan_run("daily_digest")

    try:
        daily_stats = state.get_daily_stats()
        recent_alerts = state.get_recent_alerts(hours=24)

        # LOW priority z ostatnich 24h (bez szczegółów — digest ma być zwięzły)
        # W przyszłości: przechowuj LOW alerty w DB i tu pobierz
        telegram.send_daily_digest(
            low_priority_items=[],  # TODO: store LOW items in DB
            scan_stats=daily_stats,
        )

        state.cleanup_expired()
        state.cleanup_old_social()
        log.info(f"Daily digest wysłany | stats: {daily_stats}")

    except Exception as e:
        log.error(f"daily_digest error: {e}")
        telegram.send_system_alert(f"daily_digest error: {e}", is_error=True)

    state.finish_scan_run(run_id, {"alerts_sent": 1, "errors": []})
    log.info("═══ END: daily_digest ═══")
    return {}


# ── CORE PIPELINE ────────────────────────────────────────────────────────────

def _run_token_pipeline(
    candidates: list[TokenCandidate],
    social_signals: list[SocialSignal],
    run_id: int,
    stats: dict,
) -> dict:
    """
    Wspólny pipeline dla wszystkich job types:
    Safety → Activity → Claude → Alert
    """
    if not candidates:
        return stats

    # Ogranicz liczbę kandydatów
    max_cand = cfg["general"]["max_candidates_per_run"]
    if len(candidates) > max_cand:
        log.warning(f"Przycinamy kandydatów: {len(candidates)} → {max_cand}")
        candidates = candidates[:max_cand]

    # ── WARSTWA 2: SAFETY FILTER ───────────────────────────────────────────
    passed_candidates, safety_results = batch_safety_filter(candidates, enrich_from_rugcheck=True)
    safety_map = {r.token_address: r for r in safety_results}
    stats["safety_passed"] = len(passed_candidates)

    if not passed_candidates:
        log.info("Żaden kandydat nie przeszedł safety filter")
        return stats

    # ── WARSTWA 3: ACTIVITY THRESHOLD ─────────────────────────────────────
    hits, misses = batch_activity_score(passed_candidates, social_signals=social_signals)
    stats["activity_hits"] = len(hits)

    if not hits:
        log.info("Brak HIT kandydatów po activity score")
        return stats

    # Filtruj już alertowane
    new_hits = [(c, a) for c, a in hits if not state.was_alerted(c.address)]
    log.info(f"Nowe (nie alertowane) HITs: {len(new_hits)}/{len(hits)}")

    if not new_hits:
        return stats

    # ── WARSTWA 4: CLAUDE SCORING ──────────────────────────────────────────
    scored = batch_score(new_hits, safety_map)
    stats["claude_scored"] = len(scored)

    # ── WARSTWA 5: ALERT ───────────────────────────────────────────────────
    alerts_sent = stats.get("alerts_sent", 0)
    for candidate, score_result in scored:
        activity_result = next((a for c, a in new_hits if c.address == candidate.address), None)
        if not activity_result:
            continue

        # Wyślij alert
        sent = telegram.send_token_alert(candidate, score_result, activity_result)
        if sent and score_result.priority == "HIGH":
            ntfy.send_token_push(candidate, score_result)

        if sent:
            state.mark_alerted(
                candidate.address,
                candidate.symbol,
                score_result.priority,
                score_result.narrative,
            )
            alerts_sent += 1
            log.info(
                f"  ✅ Alert wysłany: {candidate.symbol} [{score_result.priority}] — {score_result.one_liner[:50]}"
            )

    stats["alerts_sent"] = alerts_sent
    return stats


# ── HELPERS ──────────────────────────────────────────────────────────────────

def _deduplicate(candidates: list[TokenCandidate]) -> list[TokenCandidate]:
    """Usuń duplikaty po adresie, zachowaj pierwszy (zwykle najbogatszy w dane)."""
    seen = set()
    unique = []
    for c in candidates:
        if c.address and c.address not in seen:
            seen.add(c.address)
            unique.append(c)
    return unique
