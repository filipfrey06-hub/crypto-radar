#!/usr/bin/env python3
"""
Weryfikacja setupu — sprawdza importy, strukturę plików i konfigurację.
Uruchom przed pierwszym deployem.

Użycie: python scripts/verify_setup.py
"""
import sys
import os
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

errors = []
warnings = []

print("═══ Crypto Radar — weryfikacja setupu ═══\n")

# ── 1. Sprawdź strukturę plików ──────────────────────────────────────────────
print("📁 Struktura plików:")
required_files = [
    "config.yaml",
    "requirements.txt",
    ".env.example",
    ".gitignore",
    "src/__init__.py",
    "src/config.py",
    "src/pipeline.py",
    "src/scorer.py",
    "src/state.py",
    "src/fetchers/__init__.py",
    "src/fetchers/models.py",
    "src/fetchers/dexscreener.py",
    "src/fetchers/pumpfun.py",
    "src/fetchers/birdeye.py",
    "src/fetchers/reddit_fetcher.py",
    "src/fetchers/trends.py",
    "src/fetchers/rss_fetcher.py",
    "src/filters/__init__.py",
    "src/filters/safety.py",
    "src/filters/activity.py",
    "src/alerts/__init__.py",
    "src/alerts/telegram.py",
    "src/alerts/ntfy.py",
    "scripts/fast_scan.py",
    "scripts/social_scan.py",
    "scripts/whale_scan.py",
    "scripts/narrative_scan.py",
    "scripts/daily_digest.py",
    "scripts/test_alerts.py",
    ".github/workflows/fast-scan.yml",
    ".github/workflows/social-scan.yml",
    ".github/workflows/whale-scan.yml",
    ".github/workflows/narrative-scan.yml",
    ".github/workflows/daily-digest.yml",
]

for f in required_files:
    path = ROOT / f
    if path.exists():
        print(f"  ✅ {f}")
    else:
        print(f"  ❌ BRAK: {f}")
        errors.append(f"Brak pliku: {f}")

# ── 2. Sprawdź importy Python ─────────────────────────────────────────────────
print("\n📦 Importy Python:")

import_checks = [
    ("requests", "requests"),
    ("yaml", "pyyaml"),
    ("dotenv", "python-dotenv"),
    ("praw", "praw"),
    ("pytrends.request", "pytrends"),
    ("feedparser", "feedparser"),
    ("anthropic", "anthropic"),
    ("bs4", "beautifulsoup4"),
]

for module, package in import_checks:
    try:
        __import__(module)
        print(f"  ✅ {package}")
    except ImportError:
        print(f"  ❌ {package} — zainstaluj: pip install {package}")
        errors.append(f"Brak pakietu: {package}")

# ── 3. Sprawdź zmienne środowiskowe ───────────────────────────────────────────
print("\n🔑 Zmienne środowiskowe:")

# Załaduj .env jeśli istnieje
env_file = ROOT / ".env"
if env_file.exists():
    from dotenv import load_dotenv
    load_dotenv(env_file)
    print(f"  ℹ️  Załadowano .env z {env_file}")
else:
    print(f"  ⚠️  Brak .env — używam zmiennych systemowych / GitHub Secrets")
    warnings.append("Brak pliku .env — OK dla GitHub Actions, ale lokalnie skopiuj .env.example")

required_env = [
    ("TELEGRAM_BOT_TOKEN", True),
    ("TELEGRAM_CHAT_ID", True),
    ("NTFY_TOPIC", True),
    ("ANTHROPIC_API_KEY", True),
    ("REDDIT_CLIENT_ID", False),
    ("REDDIT_CLIENT_SECRET", False),
]

for var, required in required_env:
    val = os.getenv(var, "")
    if val:
        print(f"  ✅ {var} = {'*' * min(len(val), 8)}...")
    elif required:
        print(f"  ❌ {var} — WYMAGANE")
        errors.append(f"Brak zmiennej: {var}")
    else:
        print(f"  ⚠️  {var} — opcjonalne (niektóre funkcje wyłączone)")
        warnings.append(f"Brak opcjonalnego: {var}")

# ── 4. Sprawdź importy modułów projektu ──────────────────────────────────────
print("\n🔍 Importy modułów projektu:")

project_imports = [
    "src.config",
    "src.fetchers.models",
    "src.fetchers.dexscreener",
    "src.fetchers.pumpfun",
    "src.fetchers.birdeye",
    "src.fetchers.reddit_fetcher",
    "src.fetchers.trends",
    "src.fetchers.rss_fetcher",
    "src.filters.safety",
    "src.filters.activity",
    "src.scorer",
    "src.alerts.telegram",
    "src.alerts.ntfy",
    "src.state",
    "src.pipeline",
]

for mod in project_imports:
    try:
        __import__(mod)
        print(f"  ✅ {mod}")
    except Exception as e:
        print(f"  ❌ {mod}: {e}")
        errors.append(f"Import error {mod}: {e}")

# ── 5. Sprawdź config.yaml ────────────────────────────────────────────────────
print("\n⚙️  Konfiguracja (config.yaml):")
try:
    from src.config import cfg
    print(f"  ✅ config.yaml załadowany")
    print(f"  ℹ️  Min liquidity: ${cfg['safety']['min_liquidity_usd']:,}")
    print(f"  ℹ️  Volume spike: {cfg['activity']['volume_spike_multiplier']}×")
    print(f"  ℹ️  Min score for HIT: {cfg['activity']['min_score_for_hit']}")
    print(f"  ℹ️  Claude model: {cfg['claude']['model']}")
    print(f"  ℹ️  Dry run: {cfg['general']['dry_run']}")
except Exception as e:
    print(f"  ❌ Błąd config.yaml: {e}")
    errors.append(f"Config error: {e}")

# ── 6. Sprawdź DB ─────────────────────────────────────────────────────────────
print("\n💾 State Database:")
try:
    from src.state import init_db
    init_db()
    print(f"  ✅ SQLite DB zainicjalizowana: {ROOT / 'data' / 'state.db'}")
except Exception as e:
    print(f"  ❌ DB error: {e}")
    errors.append(f"DB init error: {e}")

# ── PODSUMOWANIE ───────────────────────────────────────────────────────────────
print("\n═══ PODSUMOWANIE ═══")
if errors:
    print(f"\n❌ {len(errors)} BŁĘDÓW:")
    for e in errors:
        print(f"  • {e}")
if warnings:
    print(f"\n⚠️  {len(warnings)} ostrzeżeń:")
    for w in warnings:
        print(f"  • {w}")

if not errors:
    print("\n✅ Wszystko OK! Możesz uruchomić: python scripts/test_alerts.py")
    sys.exit(0)
else:
    print("\n🔴 Napraw błędy przed deployem.")
    sys.exit(1)
