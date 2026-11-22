#!/usr/bin/env python3
"""Combine E011's Harbor jobs (the pilot and the four segments) into one score.

Usage: combine.py <job dir or segment dir> [...] [--csv out.csv]

Each argument is a Harbor job directory (jobs/e011-ese/<timestamp>/) or a folder
of them (jobs/e011-ese/). Every trial with a result.json is read. When a task has
several trials (a rerun), the latest finished one counts.

A trial counts as a result when the verifier wrote a reward and did not flag an
infrastructure failure (EFE's rule: infra_error, grader_error, no reward, or a
Harbor exception other than the agent's cutoff are rerun, not scored). Those are
listed separately and left out of the success rate.

Trials that rho-agent stopped at its cost cap (`cost_ceiling_usd`) are flagged:
their trace's `run_end` event has status `budget_exceeded`. They stay in the
score (the episode was graded as it stood, like a trial stopped at the time
limit), but are counted per suite and listed, so they can be rerun uncapped.

Standard library only.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

SUITES = [  # longest prefix first
    "embodied-safety-exam", "robocasa365", "robocasa-gr1", "robocasa", "dexhand",
    "dexjoco", "libero-max", "metaworldplus", "vlabench",
]
PREFIX = {"ese": "embodied-safety-exam"}
AGENT_CUTOFF = {"AgentTimeoutError"}  # a result, not an infrastructure failure


def suite_of(task: str) -> str:
    head = task.split("-", 1)[0]
    if head in PREFIX:
        return PREFIX[head]
    for s in SUITES:
        if task.startswith(s + "-"):
            return s
    return "other"


def trial_dirs(arg: Path):
    for result in sorted(arg.rglob("result.json")):
        if (result.parent / "config.json").exists() and result.parent.name != arg.name and (
            result.parent / "agent"
        ).exists():
            yield result.parent


def run_end_status(d: Path) -> str:
    """How rho-agent's run ended: the last `run_end` status in its trace.

    `budget_exceeded` means the cost cap stopped it (max_wall_seconds is off in
    E011, so no time cap applies). Falls back to the runner's stderr log.
    Returns "" when neither is there.
    """
    status = ""
    trace = d / "agent" / "trace.jsonl"
    if trace.exists():
        for line in trace.read_text(errors="replace").splitlines():
            if '"run_end"' not in line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "run_end":
                status = ev.get("status") or status
    if not status:
        log = d / "agent" / "stdout.txt"
        if log.exists() and "[budget_exceeded:" in log.read_text(errors="replace"):
            status = "budget_exceeded"
    return status


def read_trial(d: Path) -> dict:
    r = json.loads((d / "result.json").read_text())
    task = (r.get("task_name") or "").split("/")[-1] or d.name
    rewards = (r.get("verifier_result") or {}).get("rewards") or {}
    exc = (r.get("exception_info") or {}).get("exception_type")
    ar = r.get("agent_result") or {}
    ae = r.get("agent_execution") or {}
    minutes = None
    if ae.get("started_at") and ae.get("finished_at"):
        from datetime import datetime
        f = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))
        minutes = (f(ae["finished_at"]) - f(ae["started_at"])).total_seconds() / 60
    success = rewards.get("success", rewards.get("reward"))
    infra = (
        not rewards
        or rewards.get("infra_error") == 1
        or rewards.get("grader_error") == 1
        or (exc is not None and exc not in AGENT_CUTOFF)
    )
    outcome = next((k[len("outcome_"):] for k, v in rewards.items() if k.startswith("outcome_") and v == 1), "")
    end = run_end_status(d)
    return {
        "task": task.removesuffix("-standard"),
        "suite": suite_of(task),
        "trial": str(d),
        "finished_at": r.get("finished_at") or "",
        "success": None if infra else int(bool(success)),
        "infra": infra,
        "exception": exc or "",
        "run_end": end,
        "capped": end == "budget_exceeded",
        "ese_outcome": outcome,
        "cost_usd": ar.get("cost_usd") or 0.0,
        "input_tokens": ar.get("n_input_tokens") or 0,
        "cached_tokens": ar.get("n_cache_tokens") or 0,
        "output_tokens": ar.get("n_output_tokens") or 0,
        "agent_minutes": round(minutes, 1) if minutes is not None else None,
    }


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("paths", nargs="+", type=Path)
    p.add_argument("--csv", type=Path, help="write one row per task to this file")
    a = p.parse_args(argv)

    latest: dict[str, dict] = {}
    all_trials = 0
    for path in a.paths:
        for d in trial_dirs(path):
            t = read_trial(d)
            all_trials += 1
            if t["task"] not in latest or t["finished_at"] > latest[t["task"]]["finished_at"]:
                latest[t["task"]] = t
    rows = sorted(latest.values(), key=lambda t: (t["suite"], t["task"]))

    by = defaultdict(list)
    for t in rows:
        by[t["suite"]].append(t)
    print(f"{len(rows)} tasks from {all_trials} trials\n")
    print("| Suite | Solved | Scored | Capped | Infra failures | Cost | Mean agent min |")
    print("|---|---|---|---|---|---|---|")
    tot = {"s": 0, "n": 0, "k": 0, "i": 0, "c": 0.0}
    for s in sorted(by):
        ts = by[s]
        scored = [t for t in ts if t["success"] is not None]
        solved = sum(t["success"] for t in scored)
        capped = sum(t["capped"] for t in scored)
        infra = sum(t["infra"] for t in ts)
        cost = sum(t["cost_usd"] for t in ts)
        mins = [t["agent_minutes"] for t in scored if t["agent_minutes"] is not None]
        mean = f"{sum(mins) / len(mins):.1f}" if mins else "-"
        print(f"| {s} | {solved} | {len(scored)} | {capped} | {infra} | ${cost:.2f} | {mean} |")
        tot["s"] += solved; tot["n"] += len(scored); tot["k"] += capped; tot["i"] += infra; tot["c"] += cost
    rate = f" ({100 * tot['s'] / tot['n']:.1f}%)" if tot["n"] else ""
    print(f"| **All** | **{tot['s']}**{rate} | {tot['n']} | {tot['k']} | {tot['i']} | ${tot['c']:.2f} | |")

    capped_rows = [t for t in rows if t["capped"] and not t["infra"]]
    if capped_rows:
        print("\nStopped at the cost cap (scored as they stood; candidates for an uncapped rerun):")
        for t in capped_rows:
            verdict = "solved" if t["success"] else "failed"
            print(f"  {t['task']}: {verdict}, ${t['cost_usd']:.2f}, {t['agent_minutes']} min  ({t['trial']})")

    no_status = [t for t in rows if not t["run_end"] and not t["infra"]]
    if no_status:
        print(f"\n{len(no_status)} scored trial(s) have no run_end in their trace (agent killed at the"
              " time limit, or trace missing); whether the cap was hit cannot be read for them.")

    infra_rows = [t for t in rows if t["infra"]]
    if infra_rows:
        print("\nInfrastructure failures (rerun these; not scored):")
        for t in infra_rows:
            print(f"  {t['task']}: {t['exception'] or 'no reward / infra_error'}  ({t['trial']})")

    if a.csv:
        with a.csv.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["task"])
            w.writeheader()
            w.writerows(rows)
        print(f"\nWrote {len(rows)} rows to {a.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
