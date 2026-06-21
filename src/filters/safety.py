"""
Warstwa 2: Safety Filter — HARD STOP przed dopuszczeniem do scoringu.

Zasada: filter for safety BEFORE scoring for opportunity.
Żaden LLM tutaj — tylko deterministyczne sprawdzenia na podstawie on-chain danych.

Jeśli token nie przejdzie safety → odrzucony bez względu na narrację/momentum.
To chroni przed rug-pullami i nieuczciwymi projektami.
"""
import logging
from dataclasses import dataclass
from typing import Optional

from ..fetchers.models import TokenCandidate
from ..fetchers import rugcheck as rugcheck_api
from ..config import cfg

log = logging.getLogger(__name__)


@dataclass
class SafetyResult:
    passed: bool
    token_address: str
    token_symbol: str
    reasons: list[str]          # powody odrzucenia (jeśli passed=False)
    warnings: list[str]         # ostrzeżenia (nie blokują, ale warto wiedzieć)
    checks: dict                # wyniki poszczególnych sprawdzeń


def run_safety_filter(candidate: TokenCandidate, enrich_from_rugcheck: bool = True) -> SafetyResult:
    """
    Uruchom pełny safety filter na kandydacie.

    enrich_from_rugcheck: jeśli True, pobierz dane z RugCheck przed filtrowaniem.
    """
    scfg = cfg["safety"]
    rugcheck_score = 0
    lp_burned = False

    # Uzupełnij dane z RugCheck jeśli brakuje kluczowych pól
    if enrich_from_rugcheck and _needs_enrichment(candidate):
        try:
            report = rugcheck_api.get_token_report(candidate.address)
            if report["mint_disabled"] is not None:
                candidate.mint_disabled = report["mint_disabled"]
            if report["freeze_disabled"] is not None:
                candidate.freeze_disabled = report["freeze_disabled"]
            if report["lp_locked"] is not None:
                candidate.lp_locked = report["lp_locked"]
                candidate.lp_lock_pct = report["lp_lock_pct"]
            if report["top10_holder_pct"] > 0:
                candidate.top10_holder_pct = report["top10_holder_pct"]
            # Dodaj RugCheck score i ryzyka do extra
            rugcheck_score = report["rugcheck_score"]
            candidate.extra["rugcheck_score"] = rugcheck_score
            candidate.extra["rugcheck_risks"] = report["risks"]

            # Sprawdź czy LP jest burned (trwale spalony = bezpieczniejszy niż locked)
            risk_names_lower = [r.lower() for r in report["risks"]]
            lp_burned = any("burned" in r for r in risk_names_lower) or \
                        any("lp burned" in r for r in risk_names_lower)
            if lp_burned:
                candidate.lp_locked = True  # burned = effectively locked navždy
                candidate.lp_lock_pct = 100.0

        except Exception as e:
            log.warning(f"RugCheck enrichment failed dla {candidate.symbol}: {e}")

    reasons: list[str] = []
    warnings: list[str] = []
    checks: dict = {}

    # ── CHECK 1: Wiek tokenu ────────────────────────────────────────────────
    age_min = candidate.token_age_minutes
    min_age = scfg["min_token_age_minutes"]
    max_age = scfg["max_token_age_hours"] * 60

    checks["age_minutes"] = age_min
    if age_min > 0 and age_min < min_age:
        reasons.append(f"Za młody ({age_min:.0f} min < {min_age} min) — instant rug trap")
    if age_min > 0 and age_min > max_age:
        reasons.append(f"Za stary ({age_min/60:.0f}h > {scfg['max_token_age_hours']}h) — poza early detection window")

    # ── CHECK 2: Liquidity ──────────────────────────────────────────────────
    liq = candidate.liquidity_usd
    min_liq = scfg["min_liquidity_usd"]
    checks["liquidity_usd"] = liq
    if liq < min_liq:
        reasons.append(f"Za mała płynność (${liq:,.0f} < ${min_liq:,.0f})")

    # ── CHECK 3: Mint Authority ─────────────────────────────────────────────
    checks["mint_disabled"] = candidate.mint_disabled
    if scfg["require_mint_disabled"]:
        if candidate.mint_disabled is False:
            reasons.append("Mint authority AKTYWNE — kreator może drukować nieskończone tokeny")
        elif candidate.mint_disabled is None:
            warnings.append("Nie udało się zweryfikować mint authority — dane niekompletne")

    # ── CHECK 4: Freeze Authority ───────────────────────────────────────────
    checks["freeze_disabled"] = candidate.freeze_disabled
    if scfg["require_freeze_disabled"]:
        if candidate.freeze_disabled is False:
            reasons.append("Freeze authority AKTYWNE — kreator może zamrozić portfele holderów")
        elif candidate.freeze_disabled is None:
            warnings.append("Nie udało się zweryfikować freeze authority")

    # ── CHECK 5: LP Lock ────────────────────────────────────────────────────
    checks["lp_locked"] = candidate.lp_locked
    checks["lp_lock_pct"] = candidate.lp_lock_pct
    checks["lp_burned"] = lp_burned
    if scfg["require_lp_locked"]:
        # Wyjątek: Pump.fun tokeny przed graduation nie mają LP (bonding curve)
        if candidate.is_pump_fun and candidate.bonding_curve_pct < 100:
            warnings.append("Pump.fun pre-graduation — brak LP (to normalne), sprawdź po graduation")
        elif lp_burned:
            pass  # LP burned = trwale spalony, bezpieczniejszy niż locked
        elif candidate.lp_locked is True:
            pass  # LP locked >= 80% — OK
        elif candidate.lp_locked is False:
            lock_pct = candidate.lp_lock_pct
            # score=1 = RugCheck nie ma danych o tym tokenie (za nowy/mały) → nie karaj
            if rugcheck_score <= 1:
                warnings.append(f"LP {lock_pct:.0f}% locked — RugCheck brak danych (score=1), nie można zweryfikować")
            elif rugcheck_score >= 500 and lock_pct >= 50:
                warnings.append(f"LP tylko {lock_pct:.0f}% locked (ale RugCheck score={rugcheck_score} — OK)")
            elif rugcheck_score >= 700:
                warnings.append(f"LP {lock_pct:.0f}% locked — RugCheck score wysoki ({rugcheck_score}), akceptuję")
            else:
                reasons.append(f"LP niezablokowane (locked: {lock_pct:.0f}%, score={rugcheck_score}) — ryzyko rug")
        elif candidate.lp_locked is None and not candidate.is_pump_fun:
            # Brak danych — jeśli RugCheck score jest dobry, to tylko ostrzeżenie
            if rugcheck_score >= 500:
                warnings.append(f"LP lock nieznany — RugCheck score={rugcheck_score} (akceptowalny)")
            else:
                warnings.append("Nie udało się zweryfikować LP lock — dane niedostępne")

    # ── CHECK 6: Holder Concentration ──────────────────────────────────────
    top10_pct = candidate.top10_holder_pct
    max_top10 = scfg["max_top10_holder_pct"]
    checks["top10_holder_pct"] = top10_pct
    # Pump.fun pre-graduation: bonding curve trzyma większość tokenów — to normalne
    if candidate.is_pump_fun and candidate.bonding_curve_pct < 100:
        if top10_pct > 95:
            warnings.append(f"Pump.fun pre-graduation — top10={top10_pct:.0f}% (normalne dla bonding curve)")
    elif top10_pct == 0 or rugcheck_score <= 1:
        # Brak danych RugCheck (score=1) lub holder_pct=0 → pomiń, nie karaj za brak danych
        if top10_pct == 0:
            warnings.append("Dane o holderach niedostępne — nie można zweryfikować koncentracji")
    elif top10_pct > max_top10:
        reasons.append(f"Top 10 holderów ma {top10_pct:.0f}% supply (> {max_top10}%) — wysokie ryzyko dump")
    elif top10_pct > max_top10 * 0.85:
        warnings.append(f"Top 10 holderów ma {top10_pct:.0f}% supply — skoncentrowane")

    # ── CHECK 7: Pump.fun dev sell ──────────────────────────────────────────
    if candidate.is_pump_fun:
        dev_sold = candidate.extra.get("dev_sold", False)
        checks["dev_sold"] = dev_sold
        if dev_sold:
            warnings.append("Dev prawdopodobnie sprzedał swoje tokeny (creator_holding < 0.1%)")

    # ── CHECK 8: Holder count minimum ──────────────────────────────────────
    min_holders = cfg["birdeye"]["min_holder_count"]
    checks["holder_count"] = candidate.holder_count
    if candidate.holder_count > 0 and candidate.holder_count < min_holders:
        warnings.append(f"Mała liczba holderów ({candidate.holder_count} < {min_holders}) — mała dystrybucja")

    passed = len(reasons) == 0

    if passed:
        log.info(f"✅ SAFETY PASS: {candidate.symbol} | liq=${candidate.liquidity_usd:,.0f} | lp={candidate.lp_lock_pct:.0f}% | rc={rugcheck_score}")
    else:
        log.info(f"❌ SAFETY FAIL: {candidate.symbol} ({candidate.address[:8]}) — {'; '.join(reasons)}")

    return SafetyResult(
        passed=passed,
        token_address=candidate.address,
        token_symbol=candidate.symbol,
        reasons=reasons,
        warnings=warnings,
        checks=checks,
    )


def batch_safety_filter(
    candidates: list[TokenCandidate],
    enrich_from_rugcheck: bool = True,
) -> tuple[list[TokenCandidate], list[SafetyResult]]:
    """
    Uruchom safety filter na liście kandydatów.
    Zwraca (passed_candidates, all_results).

    Loguje podsumowanie: ile przeszło / ile odrzucono i z jakiego powodu.
    """
    passed = []
    results = []
    rejection_counts: dict[str, int] = {}

    for c in candidates:
        result = run_safety_filter(c, enrich_from_rugcheck=enrich_from_rugcheck)
        results.append(result)
        if result.passed:
            passed.append(c)
        else:
            # Zbierz statystyki powodów odrzucenia
            for reason in result.reasons:
                # Skróć reason do pierwszego słowa kluczowego
                key = reason.split("(")[0].strip()[:50]
                rejection_counts[key] = rejection_counts.get(key, 0) + 1

    log.info(
        f"Safety filter: {len(passed)}/{len(candidates)} przeszło "
        f"({len(candidates) - len(passed)} odrzuconych)"
    )

    # Pokaż top powody odrzucenia
    if rejection_counts:
        top_reasons = sorted(rejection_counts.items(), key=lambda x: -x[1])[:5]
        for reason, count in top_reasons:
            log.info(f"  └─ [{count}x] {reason}")

    return passed, results


def _needs_enrichment(candidate: TokenCandidate) -> bool:
    """Sprawdź czy kandydat potrzebuje danych z Birdeye."""
    return (
        candidate.mint_disabled is None or
        candidate.freeze_disabled is None or
        candidate.lp_locked is None or
        candidate.top10_holder_pct == 0
    )
