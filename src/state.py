"""
State Manager — SQLite dla anti-duplicate alertów + scan countery.

Zapobiega wysyłaniu tego samego alertu wielokrotnie (idempotency).
Przechowuje też statystyki per run dla daily digest.

SQLite: prosty, zero konfiguracji, działa w GitHub Actions (jako artefakt).
"""
import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from .config import cfg

log = logging.getLogger(__name__)

DB_PATH = Path(cfg["general"]["state_db_path"])


def _ensure_db_dir():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)


@contextmanager
def _db():
    """Context manager dla połączenia SQLite."""
    _ensure_db_dir()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Utwórz tabele jeśli nie istnieją. Wywołaj raz przy starcie."""
    with _db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS alerted_tokens (
                address TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                priority TEXT NOT NULL,
                narrative TEXT,
                alerted_at INTEGER NOT NULL,   -- unix timestamp
                expires_at INTEGER NOT NULL,   -- unix timestamp (kiedy można znowu alertować)
                alert_count INTEGER DEFAULT 1  -- ile razy alertowano
            );

            CREATE TABLE IF NOT EXISTS scan_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_type TEXT NOT NULL,         -- fast_scan | social_scan | whale_scan | narrative_scan
                started_at INTEGER NOT NULL,
                finished_at INTEGER,
                total_candidates INTEGER DEFAULT 0,
                safety_passed INTEGER DEFAULT 0,
                activity_hits INTEGER DEFAULT 0,
                claude_scored INTEGER DEFAULT 0,
                alerts_sent INTEGER DEFAULT 0,
                errors TEXT DEFAULT '',         -- JSON list błędów
                status TEXT DEFAULT 'running'   -- running | completed | failed
            );

            CREATE TABLE IF NOT EXISTS seen_social (
                url TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                title TEXT,
                seen_at INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_alerted_expires ON alerted_tokens(expires_at);
            CREATE INDEX IF NOT EXISTS idx_scan_runs_started ON scan_runs(started_at);
        """)
    log.debug(f"DB initialized: {DB_PATH}")


# ── ALERTED TOKENS ──────────────────────────────────────────────────────────

def was_alerted(address: str) -> bool:
    """Sprawdź czy token był już alertowany i cooldown nie minął."""
    now = int(time.time())
    with _db() as conn:
        row = conn.execute(
            "SELECT expires_at FROM alerted_tokens WHERE address = ?", (address,)
        ).fetchone()
    return row is not None and row["expires_at"] > now


def mark_alerted(address: str, symbol: str, priority: str, narrative: str = "", cooldown_hours: int = 24):
    """
    Oznacz token jako alertowany.
    cooldown_hours: jak długo nie wysyłać kolejnego alertu dla tego tokenu.
    HIGH priority = 12h cooldown, MED = 24h, LOW = 48h
    """
    cooldown_map = {"HIGH": 12, "MED": 24, "LOW": 48}
    cooldown = cooldown_map.get(priority, cooldown_hours)
    now = int(time.time())
    expires = now + cooldown * 3600

    with _db() as conn:
        conn.execute("""
            INSERT INTO alerted_tokens (address, symbol, priority, narrative, alerted_at, expires_at, alert_count)
            VALUES (?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(address) DO UPDATE SET
                priority = excluded.priority,
                alerted_at = excluded.alerted_at,
                expires_at = excluded.expires_at,
                alert_count = alert_count + 1
        """, (address, symbol, priority, narrative, now, expires))


def get_recent_alerts(hours: int = 24) -> list[dict]:
    """Pobierz alerty z ostatnich X godzin."""
    cutoff = int(time.time()) - hours * 3600
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM alerted_tokens WHERE alerted_at > ? ORDER BY alerted_at DESC",
            (cutoff,)
        ).fetchall()
    return [dict(r) for r in rows]


def cleanup_expired(max_age_days: int = 7):
    """Usuń stare wpisy (starsze niż max_age_days)."""
    cutoff = int(time.time()) - max_age_days * 86400
    with _db() as conn:
        deleted = conn.execute(
            "DELETE FROM alerted_tokens WHERE expires_at < ?", (cutoff,)
        ).rowcount
    if deleted:
        log.debug(f"Cleanup: usunięto {deleted} wygasłych wpisów")


# ── SCAN RUNS ───────────────────────────────────────────────────────────────

def start_scan_run(job_type: str) -> int:
    """Zarejestruj start nowego scan run. Zwraca run_id."""
    with _db() as conn:
        cursor = conn.execute(
            "INSERT INTO scan_runs (job_type, started_at) VALUES (?, ?)",
            (job_type, int(time.time()))
        )
        return cursor.lastrowid


def finish_scan_run(run_id: int, stats: dict, status: str = "completed"):
    """Zakończ scan run z statystykami."""
    with _db() as conn:
        conn.execute("""
            UPDATE scan_runs SET
                finished_at = ?,
                total_candidates = ?,
                safety_passed = ?,
                activity_hits = ?,
                claude_scored = ?,
                alerts_sent = ?,
                errors = ?,
                status = ?
            WHERE id = ?
        """, (
            int(time.time()),
            stats.get("total_candidates", 0),
            stats.get("safety_passed", 0),
            stats.get("activity_hits", 0),
            stats.get("claude_scored", 0),
            stats.get("alerts_sent", 0),
            json.dumps(stats.get("errors", [])),
            status,
            run_id,
        ))


def get_daily_stats() -> dict:
    """Agreguj statystyki z ostatnich 24h dla daily digest."""
    cutoff = int(time.time()) - 86400
    with _db() as conn:
        row = conn.execute("""
            SELECT
                COUNT(*) as run_count,
                SUM(total_candidates) as total_scanned,
                SUM(safety_passed) as safety_passed,
                SUM(activity_hits) as activity_hits,
                SUM(claude_scored) as claude_scored,
                SUM(alerts_sent) as alerts_sent
            FROM scan_runs
            WHERE started_at > ? AND status = 'completed'
        """, (cutoff,)).fetchone()

    return dict(row) if row else {}


# ── SEEN SOCIAL ─────────────────────────────────────────────────────────────

def was_social_seen(url: str) -> bool:
    """Sprawdź czy social signal (URL) był już przetworzony."""
    with _db() as conn:
        row = conn.execute("SELECT 1 FROM seen_social WHERE url = ?", (url,)).fetchone()
    return row is not None


def mark_social_seen(url: str, source: str, title: str = ""):
    """Oznacz social signal jako widziany."""
    with _db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO seen_social (url, source, title, seen_at) VALUES (?, ?, ?, ?)",
            (url, source, title[:200], int(time.time()))
        )


def cleanup_old_social(max_age_days: int = 3):
    """Usuń stare seen_social wpisy."""
    cutoff = int(time.time()) - max_age_days * 86400
    with _db() as conn:
        conn.execute("DELETE FROM seen_social WHERE seen_at < ?", (cutoff,))
