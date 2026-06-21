#!/usr/bin/env python3
"""Entry point: whale_scan — duże transakcje, whale movements. Co 1 godzinę."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.pipeline import setup_logging, run_whale_scan
from src import state

if __name__ == "__main__":
    setup_logging()
    state.init_db()
    result = run_whale_scan()
    sys.exit(0 if not result.get("errors") else 1)
