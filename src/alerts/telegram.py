"""
Alert layer: Telegram Bot.

Wysyła sformatowane alerty z priorytetami HIGH/MED/LOW.
Używa HTML parse mode dla rich formatting.

Telegram Bot API docs: https://core.telegram.org/bots/api#sendmessage
"""
import logging
import requests
from typing import Optional

from ..fetchers.models import TokenCandidate, SocialSignal
from ..scorer import ScoreResult
from ..filters.activity import ActivityResult
from ..config import cfg, env

log = logging.getLogger(__name__)

TG_BASE = "https://api.telegram.org"


def _send_message(text: str, parse_mode: str = "HTML", disable_web_page_preview: bool = True) -> bool:
    """Wyślij wiadomość przez Telegram Bot API."""
    if cfg["general"]["dry_run"]:
        log.info(f"[DRY RUN] Telegram message:\n{text[:200]}...")
        return True

    if not env.TELEGRAM_BOT_TOKEN or not env.TELEGRAM_CHAT_ID:
        log.warning("Brak TELEGRAM_BOT_TOKEN lub TELEGRAM_CHAT_ID — pomijam alert")
        return False

    try:
        r = requests.post(
            f"{TG_BASE}/bot{env.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": env.TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": disable_web_page_preview,
            },
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            log.error(f"Telegram API error: {data}")
            return False
        return True
    except requests.RequestException as e:
        log.error(f"Telegram send failed: {e}")
        return False


def send_token_alert(
    candidate: TokenCandidate,
    score: ScoreResult,
    activity: ActivityResult,
) -> bool:
    """
    Wyślij alert o znalezionym tokenie.
    Format zależy od priority.
    """
    acfg = cfg["alerts"]["telegram"]

    if score.priority == "HIGH" and not acfg["send_high"]:
        return False
    if score.priority == "MED" and not acfg["send_med"]:
        return False
    if score.priority == "LOW" and not acfg["send_low"]:
        return False

    emoji_map = {"HIGH": "🔴", "MED": "🟡", "LOW": "⚪"}
    emoji = emoji_map.get(score.priority, "⚪")
    risk_emoji = {"VERY_HIGH": "🚨", "HIGH": "⚠️", "MED": "📊", "LOW": "✅"}.get(score.risk_level, "❓")

    # Linki
    dexscreener_url = f"https://dexscreener.com/solana/{candidate.address}"
    rugcheck_url = f"https://rugcheck.xyz/tokens/{candidate.address}"
    pump_url = candidate.source_url if candidate.is_pump_fun else ""

    # Formatuj wiadomość
    if score.priority == "HIGH":
        msg = _format_high_alert(candidate, score, activity, emoji, risk_emoji, dexscreener_url, rugcheck_url, pump_url)
    elif score.priority == "MED":
        msg = _format_med_alert(candidate, score, activity, emoji, risk_emoji, dexscreener_url, rugcheck_url)
    else:
        msg = _format_low_alert(candidate, score, emoji, dexscreener_url)

    return _send_message(msg)


def _format_high_alert(candidate, score, activity, emoji, risk_emoji, dex_url, rug_url, pump_url) -> str:
    signals_text = "\n".join(f"  • {s}" for s in activity.signals_triggered) or "  • Brak szczegółów"
    red_flags_text = ""
    if score.red_flags:
        red_flags_text = "\n🚩 <b>Uwagi:</b>\n" + "\n".join(f"  • {f}" for f in score.red_flags[:3])

    pump_line = f"\n🎯 <a href='{pump_url}'>Pump.fun ({candidate.bonding_curve_pct:.0f}% curve)</a>" if pump_url else ""

    return f"""{emoji} <b>HIGH PRIORITY SIGNAL</b> {emoji}

🪙 <b>{candidate.name}</b> <code>${candidate.symbol}</code>
📖 {score.narrative} {'| 🥇 First mover!' if score.is_first_mover else ''}

💬 <i>{score.one_liner}</i>

📊 <b>Metryki:</b>
  • Market Cap: <code>${candidate.market_cap_usd:,.0f}</code>
  • Liquidity: <code>${candidate.liquidity_usd:,.0f}</code>
  • Volume 24h: <code>${candidate.volume_24h:,.0f}</code>
  • Wiek: <code>{candidate.token_age_minutes:.0f} min</code>
  • Holderzy: <code>{candidate.holder_count}</code>
  • Top 10 holderów: <code>{candidate.top10_holder_pct:.0f}%</code>

⚡ <b>Sygnały ({activity.score}/{activity.max_score}):</b>
{signals_text}
{red_flags_text}

{risk_emoji} Ryzyko: <b>{score.risk_level}</b>

🔗 <a href='{dex_url}'>DexScreener</a> | <a href='{rug_url}'>RugCheck</a>{pump_line}

<code>{candidate.address}</code>"""


def _format_med_alert(candidate, score, activity, emoji, risk_emoji, dex_url, rug_url) -> str:
    signals_short = " | ".join(s.split(" ", 1)[-1][:30] for s in activity.signals_triggered[:3])

    return f"""{emoji} <b>MED SIGNAL</b>: {candidate.name} <code>${candidate.symbol}</code>

<i>{score.one_liner}</i>

💹 MC: <code>${candidate.market_cap_usd:,.0f}</code> | Liq: <code>${candidate.liquidity_usd:,.0f}</code> | Vol: <code>${candidate.volume_24h:,.0f}</code>
📖 {score.narrative} | {risk_emoji} Ryzyko: {score.risk_level}
⚡ {signals_short}

<a href='{dex_url}'>DexScreener</a> | <a href='{rug_url}'>RugCheck</a>
<code>{candidate.address}</code>"""


def _format_low_alert(candidate, score, emoji, dex_url) -> str:
    return f"""{emoji} {candidate.symbol}: {score.one_liner} | MC: ${candidate.market_cap_usd:,.0f} | <a href='{dex_url}'>DS</a>"""


def send_social_alert(signal: SocialSignal) -> bool:
    """
    Alert dla political/macro sygnałów (RSS/Reddit).
    Używane gdy signal.narrative_category == 'Political' lub score > threshold.
    """
    emoji_map = {
        "Political": "🏛️",
        "AI": "🤖",
        "DePIN": "📡",
        "RWA": "🏛",
        "Gaming": "🎮",
        "Meme": "🐸",
    }
    emoji = emoji_map.get(signal.narrative_category, "📰")

    msg = f"""{emoji} <b>MACRO SIGNAL</b>: {signal.narrative_category}

📰 {signal.title}

<i>{signal.content[:300]}...</i>

🔗 <a href='{signal.url}'>Czytaj więcej</a>
📡 Źródło: {signal.source}"""

    return _send_message(msg)


def send_daily_digest(
    low_priority_items: list[tuple[TokenCandidate, ScoreResult]],
    scan_stats: dict,
) -> bool:
    """
    Dzienny digest dla LOW priority sygnałów + statystyki skanów.
    Wysyłany raz dziennie o 8:00.
    """
    total_scanned = scan_stats.get("total_scanned", 0)
    safety_passed = scan_stats.get("safety_passed", 0)
    activity_hits = scan_stats.get("activity_hits", 0)
    alerts_sent = scan_stats.get("alerts_sent", 0)

    token_lines = []
    for c, s in low_priority_items[:10]:  # max 10 w digestcie
        token_lines.append(
            f"  ⚪ <b>{c.symbol}</b>: {s.one_liner} | MC: ${c.market_cap_usd:,.0f}"
        )

    tokens_section = ""
    if token_lines:
        tokens_section = "\n\n📋 <b>LOW signals z ostatnich 24h:</b>\n" + "\n".join(token_lines)

    msg = f"""📊 <b>DZIENNY DIGEST — Crypto Radar</b>

🔍 <b>Statystyki skanów 24h:</b>
  • Zeskanowanych kandydatów: <b>{total_scanned}</b>
  • Przeszło safety filter: <b>{safety_passed}</b>
  • Hity (do Claude): <b>{activity_hits}</b>
  • Wysłanych alertów: <b>{alerts_sent}</b>{tokens_section}

⚙️ Progi: vol_spike={cfg['activity']['volume_spike_multiplier']}× | min_liq=${cfg['safety']['min_liquidity_usd']:,}"""

    return _send_message(msg)


def send_system_alert(message: str, is_error: bool = False) -> bool:
    """
    Alert systemowy — błędy, ostrzeżenia o stanie agenta.
    Fail loud, not silent.
    """
    emoji = "🚨" if is_error else "⚙️"
    msg = f"{emoji} <b>{'BŁĄD' if is_error else 'SYSTEM'}</b>: {message}"
    return _send_message(msg)


def send_test_message() -> bool:
    """Wyślij testową wiadomość żeby zweryfikować że bot działa."""
    return _send_message(
        "✅ <b>Crypto Radar — test połączenia</b>\n\nBot działa poprawnie. Alerty będą wysyłane na ten czat.",
    )
