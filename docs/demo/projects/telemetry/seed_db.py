"""Generate a deterministic telemetry.db sample database for the CLI demo."""

from __future__ import annotations

import sqlite3
from pathlib import Path

READINGS = [
    # (ts, node, battery_v, temp_c, cpu_pct)
    ("2026-11-22T14:20:00Z", "node-01", 12.4, 41.2, 34.0),
    ("2026-11-22T14:20:00Z", "node-02", 12.2, 43.8, 42.5),
    ("2026-11-22T14:20:00Z", "node-03", 12.1, 46.1, 51.0),
    ("2026-11-22T14:20:00Z", "node-04", 12.3, 68.4, 61.0),
    ("2026-11-22T14:20:00Z", "node-05", 12.5, 39.7, 28.0),
    ("2026-11-22T14:22:05Z", "node-04", 12.2, 86.3, 74.0),
    ("2026-11-22T14:25:00Z", "node-01", 12.4, 41.5, 35.0),
    ("2026-11-22T14:25:00Z", "node-02", 12.2, 44.0, 41.0),
    ("2026-11-22T14:25:00Z", "node-03", 12.0, 46.8, 53.0),
    ("2026-11-22T14:25:00Z", "node-04", 12.2, 72.1, 66.0),
    ("2026-11-22T14:25:00Z", "node-05", 12.5, 40.1, 29.0),
    ("2026-11-22T14:27:09Z", "node-04", 12.1, 89.4, 78.0),
    ("2026-11-22T14:31:02Z", "node-04", 12.1, 91.2, 81.0),
    ("2026-11-22T14:35:00Z", "node-01", 12.3, 41.8, 36.0),
    ("2026-11-22T14:35:00Z", "node-02", 12.2, 44.3, 40.5),
    ("2026-11-22T14:35:00Z", "node-03", 12.0, 47.0, 49.0),
    ("2026-11-22T14:35:00Z", "node-04", 12.1, 79.8, 72.0),
    ("2026-11-22T14:35:00Z", "node-05", 12.4, 40.4, 30.0),
]


def seed(db_path: Path = Path("telemetry.db")) -> None:
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE readings (
            ts TEXT NOT NULL,
            node TEXT NOT NULL,
            battery_v REAL NOT NULL,
            temp_c REAL NOT NULL,
            cpu_pct REAL NOT NULL
        )
        """
    )
    conn.executemany(
        "INSERT INTO readings (ts, node, battery_v, temp_c, cpu_pct) VALUES (?, ?, ?, ?, ?)",
        READINGS,
    )
    conn.commit()
    conn.close()


if __name__ == "__main__":
    seed()
