"""
Alert layer: ntfy.sh — push notifications.

Wysyła push TYLKO dla HIGH priority sygnałów (konfigurowane w config.yaml).
ntfy.sh jest darmowy i nie wymaga konta dla publicznych tematów.
Używaj prywatnego tematu (random string) żeby nikt inny nie widział alertów.

ntfy.sh docs: https://docs.ntfy.sh/
"""
import logging
import re
import requests


def _strip_emoji(text: str) -> str:
    """Usuń emoji z tekstu — nagłówki HTTP muszą być latin-1."""
    return re.sub(r'[^\x00-\x7F]+', '', text).strip()

from ..fetchers.models import TokenCandidate
from ..scorer import ScoreResult
from ..config import cfg, env

log = logging.getLogger(__name__)


def send_push(
    title: str,
    body: str,
    priority: str = "HIGH",
    tags: list[str] | None = None,
    click_url: str = "",
) -> bool:
    """
    Wyślij push notification przez ntfy.sh.

    priority: HIGH | MED | LOW (mapowane na ntfy priority 1-5)
    tags: emoji tags np. ["rotating_light", "chart_with_upwards_trend"]
    """
    ncfg = cfg["alerts"]["ntfy"]

    if ncfg["send_only_high"] and priority != "HIGH":
        log.debug(f"ntfy: pomijam {priority} (send_only_high=true)")
        return False

    if cfg["general"]["dry_run"]:
        log.info(f"[DRY RUN] ntfy push: {title} | {body[:50]}...")
        return True

    if not env.NTFY_TOPIC:
        log.warning("NTFY_TOPIC nie ustawiony — pomijam push")
        return False

    ntfy_priority = ncfg["priority_map"].get(priority, 3)
    url = f"{ncfg['base_url']}/{env.NTFY_TOPIC}"

    headers = {
        "Title": _strip_emoji(title)[:250],
        "Priority": str(ntfy_priority),
        "Tags": ",".join(tags or []),
    }
    if click_url:
        headers["Click"] = click_url

    try:
        r = requests.post(
            url,
            data=body.encode("utf-8"),
            headers=headers,
            timeout=10,
        )
        r.raise_for_status()
        log.debug(f"ntfy push sent: {title}")
        return True
    except requests.RequestException as e:
        log.error(f"ntfy push failed: {e}")
        return False


def send_token_push(candidate: TokenCandidate, score: ScoreResult) -> bool:
    """
    Push notification dla znalezionego tokenu HIGH priority.
    """
    title = f"🔴 {candidate.symbol} | {score.narrative}"
    body = (
        f"{score.one_liner}\n"
        f"MC: ${candidate.market_cap_usd:,.0f} | Liq: ${candidate.liquidity_usd:,.0f}\n"
        f"Ryzyko: {score.risk_level}"
    )

    tags = ["rotating_light", "chart_with_upwards_trend"]
    if score.is_first_mover:
        tags.append("trophy")
    if candidate.is_pump_fun:
        tags.append("rocket")

    click_url = f"https://dexscreener.com/solana/{candidate.address}"

    return send_push(
        title=title,
        body=body,
        priority=score.priority,
        tags=tags,
        click_url=click_url,
    )


def send_political_push(title: str, summary: str, url: str = "") -> bool:
    """Push dla political/macro sygnałów."""
    return send_push(
        title=f"🏛️ MACRO: {title[:200]}",
        body=summary[:500],
        priority="HIGH",
        tags=["loudspeaker", "globe_with_meridians"],
        click_url=url,
    )


def send_error_push(error_msg: str) -> bool:
    """Push dla błędów krytycznych agenta."""
    return send_push(
        title="🚨 Crypto Radar ERROR",
        body=error_msg[:500],
        priority="HIGH",
        tags=["warning"],
    )
