#!/usr/bin/env python3
"""Entry point: fast_scan — nowe tokeny Solana, volume spikes. Co 30 minut."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.pipeline import setup_logging, run_fast_scan
from src import state

if __name__ == "__main__":
    setup_logging()
    state.init_db()
    result = run_fast_scan()
    sys.exit(0 if not result.get("errors") else 1)
