"""
Warstwa 4: Claude API Narrative Scorer.

Wywoływany TYLKO dla kandydatów, które przeszły Safety + Activity filter.
Jeden call na kandydata → structured JSON output z priority, narrative, red_flags.

Używa claude-haiku (tani, szybki) chyba że wymuszono inny model.
Szacowany koszt: ~$0.002-0.004 per token (haiku pricing).
"""
import json
import logging
from dataclasses import dataclass, field
from typing import Optional

try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False

from .fetchers.models import TokenCandidate
from .filters.activity import ActivityResult
from .filters.safety import SafetyResult
from .config import cfg, env

log = logging.getLogger(__name__)


@dataclass
class ScoreResult:
    priority: str               # HIGH | MED | LOW
    narrative: str              # krótka kategoria narracyjna
    is_first_mover: bool        # czy first-mover w narracji?
    key_signals: list[str]      # kluczowe sygnały wg Claude
    red_flags: list[str]        # czerwone flagi
    one_liner: str              # jeden zdanie do Telegrama (po polsku)
    risk_level: str             # VERY_HIGH | HIGH | MED | LOW
    raw_response: str = ""      # surowa odpowiedź Claude (do debugowania)
    error: Optional[str] = None # jeśli coś poszło nie tak

    @classmethod
    def error_result(cls, error_msg: str) -> "ScoreResult":
        return cls(
            priority="LOW",
            narrative="unknown",
            is_first_mover=False,
            key_signals=[],
            red_flags=[f"Błąd scoringu: {error_msg}"],
            one_liner="Błąd analizy — sprawdź ręcznie",
            risk_level="HIGH",
            error=error_msg,
        )


def score_candidate(
    candidate: TokenCandidate,
    activity_result: ActivityResult,
    safety_result: SafetyResult,
) -> ScoreResult:
    """
    Wywołaj Claude API żeby ocenić kandydata narracyjnie.
    Zwraca ScoreResult z priority i one_liner.
    """
    if not ANTHROPIC_AVAILABLE:
        log.warning("anthropic nie zainstalowany — scorer wyłączony")
        return ScoreResult.error_result("anthropic library not installed")

    if not env.ANTHROPIC_API_KEY:
        log.warning("ANTHROPIC_API_KEY nie ustawiony — scorer wyłączony")
        return ScoreResult.error_result("brak ANTHROPIC_API_KEY")

    # Zbuduj miniaturowy brief dla Claude
    token_data = _build_token_brief(candidate, activity_result, safety_result)

    prompt = cfg["claude"]["scoring_prompt"].format(
        token_name=candidate.name,
        symbol=candidate.symbol,
        chain=candidate.chain,
        token_data=token_data,
    )

    try:
        client = anthropic.Anthropic(api_key=env.ANTHROPIC_API_KEY)
        message = client.messages.create(
            model=cfg["claude"]["model"],
            max_tokens=cfg["claude"]["max_tokens"],
            messages=[{"role": "user", "content": prompt}],
        )

        raw = message.content[0].text.strip()
        log.debug(f"Claude response dla {candidate.symbol}: {raw[:200]}")

        return _parse_score_response(raw)

    except Exception as e:
        log.error(f"Claude API error dla {candidate.symbol}: {e}")
        return ScoreResult.error_result(str(e))


def batch_score(
    hits: list[tuple[TokenCandidate, ActivityResult]],
    safety_results: dict[str, SafetyResult],
) -> list[tuple[TokenCandidate, ScoreResult]]:
    """
    Ocen wszystkie HIT kandydaty przez Claude.
    safety_results: dict {address: SafetyResult}
    Zwraca listę (candidate, score_result) posortowaną od HIGH do LOW.
    """
    scored = []
    for candidate, activity_result in hits:
        safety_result = safety_results.get(candidate.address)
        if safety_result is None:
            log.warning(f"Brak safety result dla {candidate.symbol} — pomijam")
            continue

        log.info(f"🤖 Scoring Claude: {candidate.symbol} ({candidate.address[:8]}...)")
        score = score_candidate(candidate, activity_result, safety_result)
        scored.append((candidate, score))

    # Sortuj: HIGH → MED → LOW
    priority_order = {"HIGH": 0, "MED": 1, "LOW": 2}
    scored.sort(key=lambda x: priority_order.get(x[1].priority, 3))

    log.info(
        f"Claude scoring: {len(scored)} oceniono | "
        f"HIGH={sum(1 for _, s in scored if s.priority=='HIGH')} | "
        f"MED={sum(1 for _, s in scored if s.priority=='MED')} | "
        f"LOW={sum(1 for _, s in scored if s.priority=='LOW')}"
    )
    return scored


def _build_token_brief(
    candidate: TokenCandidate,
    activity: ActivityResult,
    safety: SafetyResult,
) -> str:
    """Zbuduj zwięzły brief dla Claude z dostępnych danych."""
    lines = [
        f"Adres: {candidate.address}",
        f"Chain: {candidate.chain}",
        f"Cena: ${candidate.price_usd:.8f}",
        f"Market Cap: ${candidate.market_cap_usd:,.0f}",
        f"Liquidity: ${candidate.liquidity_usd:,.0f}",
        f"Volume 24h: ${candidate.volume_24h:,.0f}",
        f"Wiek tokenu: {candidate.token_age_minutes:.0f} minut",
        f"Holderzy: {candidate.holder_count}",
        f"Top 10 holderów: {candidate.top10_holder_pct:.0f}% supply",
        "",
        "Sygnały aktywności (które odpaliły):",
    ]
    for sig in activity.signals_triggered:
        lines.append(f"  • {sig}")

    if safety.warnings:
        lines.append("\nOstrzeżenia bezpieczeństwa (nie blokujące):")
        for w in safety.warnings:
            lines.append(f"  ⚠️ {w}")

    if candidate.is_pump_fun:
        lines.append(f"\nPump.fun: bonding curve {candidate.bonding_curve_pct:.0f}%")
        if candidate.extra.get("description"):
            lines.append(f"Opis projektu: {candidate.extra['description'][:200]}")
        if candidate.extra.get("twitter"):
            lines.append(f"Twitter: {candidate.extra['twitter']}")

    if candidate.reddit_mentions_24h > 0:
        lines.append(f"\nReddit wzmianki 24h: {candidate.reddit_mentions_24h}")

    if candidate.google_trends_score > 0:
        lines.append(f"Google Trends score: {candidate.google_trends_score}/100")

    return "\n".join(lines)


def _parse_score_response(raw: str) -> ScoreResult:
    """Parsuj JSON response od Claude."""
    # Wyciągnij JSON z odpowiedzi (Claude czasem dodaje tekst przed/po)
    start = raw.find("{")
    end = raw.rfind("}") + 1
    if start == -1 or end == 0:
        log.warning(f"Claude nie zwrócił JSON: {raw[:100]}")
        return ScoreResult.error_result(f"Brak JSON w odpowiedzi: {raw[:50]}")

    try:
        data = json.loads(raw[start:end])
        return ScoreResult(
            priority=data.get("priority", "LOW").upper(),
            narrative=data.get("narrative", "unknown"),
            is_first_mover=bool(data.get("is_first_mover", False)),
            key_signals=data.get("key_signals", []),
            red_flags=data.get("red_flags", []),
            one_liner=data.get("one_liner", "Brak opisu"),
            risk_level=data.get("risk_level", "HIGH").upper(),
            raw_response=raw,
        )
    except json.JSONDecodeError as e:
        log.warning(f"JSON decode error: {e} | raw: {raw[:100]}")
        return ScoreResult.error_result(f"JSON parse error: {e}")
