"""Fleet health check against telemetry.db and policy.toml."""

from __future__ import annotations

import sqlite3
import tomllib
from pathlib import Path


def load_limits(policy_path: Path = Path("policy.toml")) -> dict[str, float]:
    with policy_path.open("rb") as f:
        return tomllib.load(f)["limits"]


def latest_readings(db_path: Path = Path("telemetry.db")) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT node,
               MIN(battery_v) AS min_battery_v,
               MAX(temp_c)    AS peak_temp_c,
               MAX(cpu_pct)   AS peak_cpu_pct
        FROM readings
        GROUP BY node
        ORDER BY node
        """
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def check_fleet(
    db_path: Path = Path("telemetry.db"),
    policy_path: Path = Path("policy.toml"),
) -> list[tuple[str, str, str]]:
    limits = load_limits(policy_path)
    results: list[tuple[str, str, str]] = []
    for row in latest_readings(db_path):
        issues: list[str] = []
        if row["min_battery_v"] < limits["min_battery_v"]:
            issues.append(f"battery {row['min_battery_v']:.1f}V < {limits['min_battery_v']:.1f}V")
        if row["peak_cpu_pct"] > limits["max_cpu_pct"]:
            issues.append(f"cpu {row['peak_cpu_pct']:.0f}% > {limits['max_cpu_pct']:.0f}%")
        status = "ALERT" if issues else "OK"
        detail = ", ".join(issues) if issues else "nominal"
        results.append((row["node"], status, detail))
    return results


def main() -> None:
    results = check_fleet()
    for node, status, detail in results:
        print(f"{node}  {status:<5}  {detail}")


if __name__ == "__main__":
    main()
