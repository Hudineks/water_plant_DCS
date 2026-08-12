"""SQLite historian: one flat table, ts/unit/tag/value. Direct writes, no
ORM, this is a demo-scale logger not a time-series database.
"""
from __future__ import annotations

import sqlite3
import time


class Historian:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS history (
                ts REAL NOT NULL,
                unit TEXT NOT NULL,
                tag TEXT NOT NULL,
                value TEXT NOT NULL
            )
            """
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_history_ts ON history(ts)")
        self._conn.commit()

    def log_values(self, unit: str, values: dict[str, object], ts: float | None = None) -> None:
        ts = ts if ts is not None else time.time()
        rows = [(ts, unit, tag, str(value)) for tag, value in values.items()]
        self._conn.executemany("INSERT INTO history (ts, unit, tag, value) VALUES (?, ?, ?, ?)", rows)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
