"""SQLite storage layer for trend data.

Schema:
- trends: current state per (platform, native_id)
- trend_snapshots: time-series for velocity/decay scoring
- collection_runs: per-cycle audit log

Stdlib sqlite3 (no SQLAlchemy — keep it light for v1).
"""
from __future__ import annotations

import json
import structlog
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from ..normalizer.schema import Trend, TrendSignal

logger = structlog.get_logger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS trends (
    id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    name TEXT NOT NULL,
    trend_type TEXT NOT NULL,
    url TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    latest_score REAL,
    latest_rank INTEGER,
    metadata_json TEXT,
    cross_platform_key TEXT,
    normalized_name TEXT
);

CREATE INDEX IF NOT EXISTS idx_trends_platform ON trends(platform);
CREATE INDEX IF NOT EXISTS idx_trends_xpkey ON trends(cross_platform_key);
CREATE INDEX IF NOT EXISTS idx_trends_nname ON trends(normalized_name);
CREATE INDEX IF NOT EXISTS idx_trends_lastrecent ON trends(last_seen DESC);

CREATE TABLE IF NOT EXISTS trend_snapshots (
    trend_id TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    score REAL NOT NULL,
    rank INTEGER,
    raw_json TEXT,
    PRIMARY KEY (trend_id, captured_at),
    FOREIGN KEY (trend_id) REFERENCES trends(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_snapshots_time ON trend_snapshots(captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_snapshots_trend_time ON trend_snapshots(trend_id, captured_at DESC);

CREATE TABLE IF NOT EXISTS collection_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    platform TEXT NOT NULL,
    status TEXT,
    items_collected INTEGER DEFAULT 0,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_recent ON collection_runs(started_at DESC);
"""


class Storage:
    """Thin SQLite wrapper. All writes are serialized via a single connection."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._snap_nonce = 0
        logger.info("storage.initialized", path=str(self.db_path))

    @contextmanager
    def cursor(self):
        cur = self._conn.cursor()
        try:
            yield cur
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass

    def _next_snap_nonce(self) -> int:
        self._snap_nonce = (self._snap_nonce + 1) % 1_000_000
        return self._snap_nonce

    # ---- writes ----

    def upsert_trend(self, trend: Trend) -> None:
        """Insert or update a trend's current state. The trend's latest
        signal is also written to the snapshots table for time-series."""
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT INTO trends (id, platform, name, trend_type, url,
                                    first_seen, last_seen, latest_score,
                                    latest_rank, metadata_json,
                                    cross_platform_key, normalized_name)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,
                    url=COALESCE(excluded.url, trends.url),
                    last_seen=excluded.last_seen,
                    latest_score=excluded.latest_score,
                    latest_rank=excluded.latest_rank,
                    metadata_json=excluded.metadata_json,
                    cross_platform_key=excluded.cross_platform_key,
                    normalized_name=excluded.normalized_name
                """,
                (
                    trend.id,
                    trend.platform,
                    trend.name,
                    trend.trend_type,
                    trend.url,
                    trend.first_seen.isoformat(),
                    trend.last_seen.isoformat(),
                    trend.score,
                    trend.signals[-1].rank if trend.signals else None,
                    json.dumps(trend.metadata),
                    trend.cross_platform_key,
                    trend.normalized_name,
                ),
            )
            # Snapshot — one row per upsert, identified by (trend_id, captured_at).
            # To ensure distinct rows when two upserts land in the same
            # microsecond, we use a per-insert offset derived from the rowid.
            if trend.signals:
                sig = trend.signals[-1]
                from datetime import datetime, timezone
                # Add a tiny microsecond offset so two same-instant snapshots
                # both get persisted. The offset is derived from a
                # monotonically increasing counter stored on the connection.
                counter = self._next_snap_nonce()
                base = datetime.now(tz=timezone.utc)
                snap_time = base.replace(microsecond=(base.microsecond + counter) % 1_000_000)
                cur.execute(
                    """
                    INSERT OR REPLACE INTO trend_snapshots
                        (trend_id, captured_at, score, rank, raw_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        trend.id,
                        snap_time.isoformat(),
                        sig.score,
                        sig.rank,
                        json.dumps(sig.raw) if sig.raw else None,
                    ),
                )

    def upsert_many(self, trends: Iterable[Trend]) -> int:
        count = 0
        for t in trends:
            self.upsert_trend(t)
            count += 1
        return count

    def record_run(
        self,
        platform: str,
        started_at: datetime,
        finished_at: datetime | None,
        status: str,
        items_collected: int,
        error: str | None = None,
    ) -> int:
        with self.cursor() as cur:
            cur.execute(
                """INSERT INTO collection_runs
                       (started_at, finished_at, platform, status,
                        items_collected, error)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    started_at.isoformat(),
                    finished_at.isoformat() if finished_at else None,
                    platform,
                    status,
                    items_collected,
                    error,
                ),
            )
            return cur.lastrowid

    # ---- reads ----

    def list_trends(
        self,
        platform: str | None = None,
        limit: int = 100,
        min_score: float | None = None,
    ) -> list[dict]:
        sql = "SELECT * FROM trends WHERE 1=1"
        params: list = []
        if platform:
            sql += " AND platform = ?"
            params.append(platform)
        if min_score is not None:
            sql += " AND latest_score >= ?"
            params.append(min_score)
        sql += " ORDER BY last_seen DESC LIMIT ?"
        params.append(limit)
        with self.cursor() as cur:
            cur.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]

    def get_trend(self, trend_id: str) -> dict | None:
        with self.cursor() as cur:
            cur.execute("SELECT * FROM trends WHERE id = ?", (trend_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    def get_snapshots(self, trend_id: str, limit: int = 100) -> list[dict]:
        with self.cursor() as cur:
            cur.execute(
                """SELECT * FROM trend_snapshots
                   WHERE trend_id = ?
                   ORDER BY captured_at DESC
                   LIMIT ?""",
                (trend_id, limit),
            )
            return [dict(row) for row in cur.fetchall()]

    def last_run(self, platform: str) -> dict | None:
        with self.cursor() as cur:
            cur.execute(
                """SELECT * FROM collection_runs
                   WHERE platform = ?
                   ORDER BY started_at DESC LIMIT 1""",
                (platform,),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def health(self) -> dict:
        with self.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM trends")
            trends_total = cur.fetchone()["c"]
            cur.execute(
                """SELECT platform, COUNT(*) AS c FROM trends
                   GROUP BY platform"""
            )
            by_platform = {r["platform"]: r["c"] for r in cur.fetchall()}
            cur.execute(
                """SELECT platform, status, MAX(started_at) AS last_started
                   FROM collection_runs
                   GROUP BY platform"""
            )
            runs = {r["platform"]: dict(r) for r in cur.fetchall()}
        return {
            "trends_total": trends_total,
            "by_platform": by_platform,
            "last_runs": runs,
            "now": datetime.now(tz=timezone.utc).isoformat(),
        }
