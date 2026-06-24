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
            CREATE TABLE IF NOT EXISTS metadao_proposals (
                proposal_id TEXT PRIMARY KEY,
                title TEXT,
                state TEXT,
                first_seen_at INTEGER NOT NULL,
                last_seen_at INTEGER NOT NULL,
                alerted_new INTEGER DEFAULT 0,   -- 1 jeśli wysłano alert "nowa propozycja"
                alerted_resolved INTEGER DEFAULT 0  -- 1 jeśli wysłano alert o rozstrzygnięciu
            );

            CREATE TABLE IF NOT EXISTS avici_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                price_usd REAL,
                price_change_h1 REAL,
                price_change_h24 REAL,
                volume_h24 REAL,
                liquidity_usd REAL,
                market_cap_usd REAL,
                recorded_at INTEGER NOT NULL
            );

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


# ── METADAO PROPOSALS ───────────────────────────────────────────────────────

def get_known_proposal_ids() -> set[str]:
    """Zwróć zestaw ID propozycji które już widzieliśmy."""
    with _db() as conn:
        rows = conn.execute("SELECT proposal_id FROM metadao_proposals").fetchall()
    return {r["proposal_id"] for r in rows}


def mark_proposal_seen(proposal_id: str, title: str, state: str):
    """Dodaj/zaktualizuj propozycję w DB."""
    now = int(time.time())
    with _db() as conn:
        conn.execute("""
            INSERT INTO metadao_proposals (proposal_id, title, state, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(proposal_id) DO UPDATE SET
                state = excluded.state,
                last_seen_at = excluded.last_seen_at
        """, (proposal_id, title[:200], state, now, now))


def was_proposal_new_alerted(proposal_id: str) -> bool:
    with _db() as conn:
        row = conn.execute(
            "SELECT alerted_new FROM metadao_proposals WHERE proposal_id = ?",
            (proposal_id,)
        ).fetchone()
    return bool(row and row["alerted_new"])


def mark_proposal_new_alerted(proposal_id: str):
    with _db() as conn:
        conn.execute(
            "UPDATE metadao_proposals SET alerted_new = 1 WHERE proposal_id = ?",
            (proposal_id,)
        )


def was_proposal_resolved_alerted(proposal_id: str) -> bool:
    with _db() as conn:
        row = conn.execute(
            "SELECT alerted_resolved FROM metadao_proposals WHERE proposal_id = ?",
            (proposal_id,)
        ).fetchone()
    return bool(row and row["alerted_resolved"])


def mark_proposal_resolved_alerted(proposal_id: str):
    with _db() as conn:
        conn.execute(
            "UPDATE metadao_proposals SET alerted_resolved = 1 WHERE proposal_id = ?",
            (proposal_id,)
        )


def get_rpc_proposal_count() -> int:
    """Pobierz ostatnio zapisaną liczbę kont Autocrat (dla RPC fallback)."""
    with _db() as conn:
        row = conn.execute(
            "SELECT proposal_id FROM metadao_proposals WHERE proposal_id LIKE 'rpc_count_%'"
        ).fetchone()
    if not row:
        return 0
    try:
        return int(row["proposal_id"].replace("rpc_count_", ""))
    except Exception:
        return 0


# ── AVICI SNAPSHOTS ─────────────────────────────────────────────────────────

def save_avici_snapshot(price_usd: float, price_change_h1: float, price_change_h24: float,
                        volume_h24: float, liquidity_usd: float, market_cap_usd: float):
    """Zapisz snapshot ceny AVICI (do weekly digest i analizy)."""
    with _db() as conn:
        conn.execute("""
            INSERT INTO avici_snapshots
            (price_usd, price_change_h1, price_change_h24, volume_h24, liquidity_usd, market_cap_usd, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (price_usd, price_change_h1, price_change_h24, volume_h24, liquidity_usd, market_cap_usd,
              int(time.time())))


def get_avici_price_7d_ago() -> Optional[float]:
    """Pobierz cenę AVICI sprzed ~7 dni (dla weekly digest)."""
    cutoff_min = int(time.time()) - 7 * 86400 - 3600  # -7d ±1h
    cutoff_max = int(time.time()) - 7 * 86400 + 3600
    with _db() as conn:
        row = conn.execute(
            "SELECT price_usd FROM avici_snapshots WHERE recorded_at BETWEEN ? AND ? ORDER BY recorded_at DESC LIMIT 1",
            (cutoff_min, cutoff_max)
        ).fetchone()
    return float(row["price_usd"]) if row else None


def get_avici_snapshots_7d() -> list[dict]:
    """Pobierz wszystkie snapshoty AVICI z ostatnich 7 dni."""
    cutoff = int(time.time()) - 7 * 86400
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM avici_snapshots WHERE recorded_at > ? ORDER BY recorded_at ASC",
            (cutoff,)
        ).fetchall()
    return [dict(r) for r in rows]


def cleanup_old_avici_snapshots(max_days: int = 30):
    """Usuń snapshoty starsze niż X dni."""
    cutoff = int(time.time()) - max_days * 86400
    with _db() as conn:
        conn.execute("DELETE FROM avici_snapshots WHERE recorded_at < ?", (cutoff,))
