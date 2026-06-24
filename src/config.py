"""
Ładuje config.yaml i zmienne środowiskowe (.env / GitHub Secrets).
Wszystkie moduły importują stąd cfg i env — nie z osobnych plików.
"""
import os
import yaml
from pathlib import Path
from dotenv import load_dotenv

# Załaduj .env jeśli istnieje (lokalnie), w GitHub Actions są Secrets
_root = Path(__file__).parent.parent
load_dotenv(_root / ".env")

def load_config(path: str | Path | None = None) -> dict:
    cfg_path = Path(path) if path else _root / "config.yaml"
    with open(cfg_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

cfg = load_config()

class Env:
    """Wszystkie zmienne środowiskowe w jednym miejscu. Wybucha przy starcie jeśli czegoś brakuje."""
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str   = os.getenv("TELEGRAM_CHAT_ID", "")
    NTFY_TOPIC: str         = os.getenv("NTFY_TOPIC", "crypto-radar")
    ANTHROPIC_API_KEY: str  = os.getenv("ANTHROPIC_API_KEY", "")
    REDDIT_CLIENT_ID: str   = os.getenv("REDDIT_CLIENT_ID", "")
    REDDIT_CLIENT_SECRET: str = os.getenv("REDDIT_CLIENT_SECRET", "")
    REDDIT_USER_AGENT: str  = os.getenv("REDDIT_USER_AGENT", "crypto-radar:v2.0")
    # AVICI Poland — osobny bot i czat Telegram dla raportów AVICI
    AVICI_TELEGRAM_BOT_TOKEN: str = os.getenv("AVICI_TELEGRAM_BOT_TOKEN", "")
    AVICI_TELEGRAM_CHAT_ID: str   = os.getenv("AVICI_TELEGRAM_CHAT_ID", "")

    @classmethod
    def validate(cls, require_claude: bool = True, require_reddit: bool = True):
        """Rzuca ValueError jeśli brakuje wymaganych zmiennych."""
        missing = []
        if not cls.TELEGRAM_BOT_TOKEN:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not cls.TELEGRAM_CHAT_ID:
            missing.append("TELEGRAM_CHAT_ID")
        if require_claude and not cls.ANTHROPIC_API_KEY:
            missing.append("ANTHROPIC_API_KEY")
        if require_reddit and not cls.REDDIT_CLIENT_ID:
            missing.append("REDDIT_CLIENT_ID")
        if missing:
            raise ValueError(f"Brakuje zmiennych środowiskowych: {', '.join(missing)}")

env = Env()
