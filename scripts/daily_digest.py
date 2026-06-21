#!/usr/bin/env python3
"""Entry point: daily_digest — statystyki 24h + LOW sygnały. Raz dziennie o 8:00."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.pipeline import setup_logging, run_daily_digest
from src import state

if __name__ == "__main__":
    setup_logging()
    state.init_db()
    run_daily_digest()
    sys.exit(0)
