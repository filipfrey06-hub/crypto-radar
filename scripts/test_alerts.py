#!/usr/bin/env python3
"""
Test połączenia z Telegram i ntfy.sh.
Uruchom PRZED pierwszym deployem żeby sprawdzić czy secrets są poprawne.

Użycie:
  python scripts/test_alerts.py
  python scripts/test_alerts.py --dry-run
"""
import sys
import argparse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import cfg, env
from src.alerts.telegram import send_test_message, send_system_alert
from src.alerts.ntfy import send_push
from src.fetchers.models import TokenCandidate
from src.scorer import ScoreResult
from src.filters.activity import ActivityResult

def main():
    parser = argparse.ArgumentParser(description="Test alertów Crypto Radar")
    parser.add_argument("--dry-run", action="store_true", help="Nie wysyłaj, tylko loguj")
    args = parser.parse_args()

    if args.dry_run:
        cfg["general"]["dry_run"] = True
        print("🔵 DRY RUN — alerty nie będą wysyłane")

    print("\n─── Test 1: Telegram połączenie ───")
    ok = send_test_message()
    print(f"  {'✅ OK' if ok else '❌ FAIL'} — Telegram")

    print("\n─── Test 2: Telegram token alert (przykładowy HIGH) ───")
    from src.alerts.telegram import send_token_alert

    mock_candidate = TokenCandidate(
        address="TestAddr123456789",
        symbol="RADAR",
        name="Crypto Radar Test",
        chain="solana",
        price_usd=0.0000123,
        market_cap_usd=420000,
        liquidity_usd=85000,
        volume_24h=1200000,
        token_age_minutes=95,
        holder_count=342,
        top10_holder_pct=28.5,
        mint_disabled=True,
        freeze_disabled=True,
        lp_locked=True,
        is_pump_fun=True,
        bonding_curve_pct=67.3,
        source="test",
        source_url="https://pump.fun/TestAddr123456789",
    )

    mock_score = ScoreResult(
        priority="HIGH",
        narrative="AI agent memecoin",
        is_first_mover=True,
        key_signals=["Volume spike 4.2×", "Bonding curve 67%", "Reddit spike"],
        red_flags=[],
        one_liner="Nowy AI memecoin z szybko rosnącą bonding curve i whale inflow",
        risk_level="HIGH",
    )

    mock_activity = ActivityResult(
        is_hit=True,
        score=4,
        max_score=6,
        signals_triggered=[
            "📊 Volume spike 4.2×",
            "📈 Bonding curve 67.3% (graduation = 100%)",
            "💬 3 wzmianki w monitorowanych źródłach",
            "⚡ Pump.fun sweet spot (67% curve)",
        ],
        signals_checked={},
        candidate_address="TestAddr123456789",
        candidate_symbol="RADAR",
    )

    ok2 = send_token_alert(mock_candidate, mock_score, mock_activity)
    print(f"  {'✅ OK' if ok2 else '❌ FAIL'} — Token alert (HIGH)")

    print("\n─── Test 3: ntfy.sh push ───")
    ok3 = send_push(
        title="🔴 TEST: Crypto Radar działa!",
        body="To jest testowy push notification. Jeśli to widzisz — ntfy.sh działa poprawnie.",
        priority="HIGH",
        tags=["white_check_mark"],
    )
    print(f"  {'✅ OK' if ok3 else '❌ FAIL'} — ntfy.sh push")

    print(f"\n─── Zmienne środowiskowe ───")
    print(f"  TELEGRAM_BOT_TOKEN: {'✅ ustawiony' if env.TELEGRAM_BOT_TOKEN else '❌ brak'}")
    print(f"  TELEGRAM_CHAT_ID:   {'✅ ustawiony' if env.TELEGRAM_CHAT_ID else '❌ brak'}")
    print(f"  NTFY_TOPIC:         {'✅ ustawiony' if env.NTFY_TOPIC else '❌ brak'}")
    print(f"  ANTHROPIC_API_KEY:  {'✅ ustawiony' if env.ANTHROPIC_API_KEY else '❌ brak'}")
    print(f"  REDDIT_CLIENT_ID:   {'✅ ustawiony' if env.REDDIT_CLIENT_ID else '❌ brak'}")

    all_ok = ok and ok2 and ok3
    print(f"\n{'✅ Wszystkie testy OK!' if all_ok else '⚠️ Niektóre testy nie przeszły — sprawdź logi powyżej'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
