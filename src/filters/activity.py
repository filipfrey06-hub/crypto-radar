"""
Warstwa 3: Activity Threshold Scorer — numeryczne progi bez LLM.

Każde kryterium = 1 punkt. Minimalna suma punktów (min_score_for_hit) = HIT → do Claude.
Żadnych kosztownych API calls tutaj — tylko math na danych już zebranych.

To jest kluczowy "tani filtr" między safety a drogim LLM scoring.
"""
import logging
from dataclasses import dataclass, field

from ..fetchers.models import TokenCandidate, SocialSignal
from ..config import cfg

log = logging.getLogger(__name__)


@dataclass
class ActivityResult:
    is_hit: bool                       # True jeśli suma >= min_score_for_hit
    score: int                         # suma punktów
    max_score: int                     # ile kryteriów sprawdzono
    signals_triggered: list[str]       # które sygnały odpaliły
    signals_checked: dict              # wartości poszczególnych metryk
    candidate_address: str = ""
    candidate_symbol: str = ""


def score_token(
    candidate: TokenCandidate,
    social_signals: list[SocialSignal] | None = None,
) -> ActivityResult:
    """
    Przyznaj punkty kandydatowi na podstawie activity signals.
    social_signals — opcjonalne, przekazane z Reddit/Trends fetchera.
    """
    acfg = cfg["activity"]
    weights = acfg["weights"]
    social_signals = social_signals or []

    score = 0
    triggered: list[str] = []
    checked: dict = {}

    # ── SYGNAŁ 1: Volume Spike ──────────────────────────────────────────────
    vol_24h = candidate.volume_24h
    vol_avg = candidate.volume_7d_avg if candidate.volume_7d_avg > 0 else vol_24h / 7
    checked["volume_24h"] = vol_24h
    checked["volume_7d_avg"] = vol_avg

    if vol_avg > 0:
        vol_multiplier = vol_24h / vol_avg
        checked["volume_spike_multiplier"] = round(vol_multiplier, 2)
        if vol_multiplier >= acfg["volume_spike_multiplier"]:
            score += weights["volume_spike"]
            triggered.append(f"📊 Volume spike {vol_multiplier:.1f}×")
    else:
        # Jeśli nie ma historii, sprawdź czy volume > próg absolutny
        if vol_24h >= cfg["dexscreener"]["min_volume_24h"] * 5:
            score += weights["volume_spike"]
            triggered.append(f"📊 Volume {vol_24h:,.0f}$ (brak historii baseline)")

    # ── SYGNAŁ 2: Social Spike (Reddit/Trends) ──────────────────────────────
    reddit_mentions = candidate.reddit_mentions_24h
    reddit_avg = candidate.reddit_mentions_7d_avg
    trends_score = candidate.google_trends_score
    checked["reddit_mentions_24h"] = reddit_mentions
    checked["google_trends_score"] = trends_score

    # Sprawdź social signals przekazane z zewnątrz
    token_symbol_lower = candidate.symbol.lower()
    token_name_lower = candidate.name.lower()
    relevant_socials = [
        s for s in social_signals
        if (token_symbol_lower in s.title.lower() or
            token_name_lower in s.title.lower() or
            token_symbol_lower in s.content.lower() or
            f"${candidate.symbol.upper()}" in s.title or
            f"${candidate.symbol.upper()}" in s.content)
    ]
    checked["relevant_social_signals"] = len(relevant_socials)

    social_spike = False
    if reddit_mentions > 0 and reddit_avg > 0:
        social_multiplier = reddit_mentions / reddit_avg
        checked["social_spike_multiplier"] = round(social_multiplier, 2)
        if social_multiplier >= acfg["social_mention_spike_multiplier"]:
            social_spike = True
            triggered.append(f"💬 Social spike {social_multiplier:.1f}× na Reddit")

    if trends_score >= cfg["google_trends"]["spike_threshold"]:
        social_spike = True
        triggered.append(f"🔍 Google Trends spike {trends_score}/100")

    if len(relevant_socials) >= 2:
        social_spike = True
        triggered.append(f"💬 {len(relevant_socials)} wzmianek w monitorowanych źródłach")

    if social_spike:
        score += weights["social_spike"]

    # ── SYGNAŁ 3: Whale Inflow ──────────────────────────────────────────────
    whale_inflow = float(candidate.extra.get("whale_inflow_usd", 0))
    checked["whale_inflow_usd"] = whale_inflow
    if whale_inflow >= acfg["whale_inflow_usd"]:
        score += weights["whale_inflow"]
        triggered.append(f"🐋 Whale inflow ${whale_inflow:,.0f}")

    # ── SYGNAŁ 4: Pump.fun Bonding Curve ───────────────────────────────────
    if candidate.is_pump_fun:
        curve_pct = candidate.bonding_curve_pct
        checked["bonding_curve_pct"] = curve_pct
        if curve_pct >= acfg["pump_fun_bonding_curve_pct"]:
            score += weights["bonding_curve"]
            triggered.append(f"📈 Bonding curve {curve_pct:.0f}% (graduation = 100%)")
    else:
        checked["bonding_curve_pct"] = None

    # ── SYGNAŁ 5: Narrative Keyword Match ───────────────────────────────────
    narrative_hit, matched_narratives = _check_narrative_keywords(candidate)
    checked["narrative_keywords_matched"] = matched_narratives
    if narrative_hit:
        score += weights["narrative_keyword"]
        triggered.append(f"🎯 Narracja: {', '.join(matched_narratives)}")

    # ── SYGNAŁ 6: New Token + Early Entry ──────────────────────────────────
    age_min = candidate.token_age_minutes
    checked["token_age_minutes"] = age_min
    # Bonus za early: tokeny 30min - 4h stare (liquidity już sprawdzone w safety)
    is_early = 30 <= age_min <= 240 and candidate.liquidity_usd >= 15000
    # Lub pump.fun w "sweet spot": 40-80% bonding curve
    is_pump_sweet_spot = candidate.is_pump_fun and 40 <= candidate.bonding_curve_pct <= 80
    if is_early or is_pump_sweet_spot:
        score += weights["new_token_early"]
        reason = f"⚡ Early entry ({age_min:.0f}min stary)" if is_early else f"⚡ Pump.fun sweet spot ({candidate.bonding_curve_pct:.0f}% curve)"
        triggered.append(reason)

    # ── WYNIK ───────────────────────────────────────────────────────────────
    min_score = acfg["min_score_for_hit"]
    is_hit = score >= min_score

    if is_hit:
        log.info(f"🎯 HIT: {candidate.symbol} score={score}/{len(weights)} — {', '.join(triggered)}")
    else:
        log.info(f"  MISS: {candidate.symbol} score={score}/{len(weights)} | sygnały={triggered or ['brak']}")

    return ActivityResult(
        is_hit=is_hit,
        score=score,
        max_score=len(weights),
        signals_triggered=triggered,
        signals_checked=checked,
        candidate_address=candidate.address,
        candidate_symbol=candidate.symbol,
    )


def batch_activity_score(
    candidates: list[TokenCandidate],
    social_signals: list[SocialSignal] | None = None,
) -> tuple[list[tuple[TokenCandidate, ActivityResult]], list[tuple[TokenCandidate, ActivityResult]]]:
    """
    Ocen wszystkich kandydatów, rozdziel na HITs i MISSes.
    Zwraca (hits, misses) jako tuple (candidate, result).
    """
    hits = []
    misses = []

    for c in candidates:
        result = score_token(c, social_signals=social_signals)
        if result.is_hit:
            hits.append((c, result))
        else:
            misses.append((c, result))

    log.info(
        f"Activity scorer: {len(hits)} HIT / {len(misses)} MISS "
        f"(z {len(candidates)} kandydatów po safety filtrze)"
    )
    return hits, misses


def _check_narrative_keywords(candidate: TokenCandidate) -> tuple[bool, list[str]]:
    """
    Sprawdź czy token pasuje do monitorowanych narracji.
    Sprawdza: symbol, name, description z extra danych.
    """
    text = " ".join([
        candidate.symbol,
        candidate.name,
        candidate.extra.get("description", ""),
        candidate.extra.get("twitter", ""),
        candidate.extra.get("website", ""),
    ]).lower()

    matched = []
    for cat, keywords in cfg["narratives"]["keywords"].items():
        for kw in keywords:
            if kw.lower() in text:
                matched.append(cat)
                break  # jedna kategoria = jeden punkt

    return len(matched) > 0, list(set(matched))
