"""Compare two Harbor job directories trial by trial: tokens, cost, calls, reward.

    python3 experiments/tools/compare_trials.py <job-dir-A> <job-dir-B> [--csv out.csv]

Each argument is a job directory, or a folder of them (the newest is used).
Prints a per-task table (the mean over attempts) and per-arm totals.
Standard library only.
"""

from __future__ import annotations

import csv
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean

FIELDS = [
    "reward",
    "cost_usd",
    "input",
    "cached",
    "uncached",
    "output",
    "reasoning",
    "model_calls",
    "tool_calls",
    "compactions",
    "agent_minutes",
]
SHOWN = [
    "reward",
    "cost_usd",
    "uncached",
    "cached",
    "output",
    "reasoning",
    "model_calls",
    "tool_calls",
    "agent_minutes",
]


def job_dir(path: Path) -> Path:
    if (path / "config.json").exists():
        return path
    runs = sorted(p for p in path.iterdir() if p.is_dir() and (p / "config.json").exists())
    if not runs:
        sys.exit(f"No Harbor job found in {path}")
    return runs[-1]


def _minutes(phase: dict | None) -> float | None:
    if not phase or not phase.get("started_at") or not phase.get("finished_at"):
        return None
    start = datetime.fromisoformat(phase["started_at"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(phase["finished_at"].replace("Z", "+00:00"))
    return (end - start).total_seconds() / 60


def trial(path: Path) -> dict:
    result = json.loads((path / "result.json").read_text())
    agent = path / "agent"
    tokens = {}
    if (agent / "tokens.json").exists():
        tokens = json.loads((agent / "tokens.json").read_text())
    tools: Counter[str] = Counter()
    calls = compactions = 0
    status = offered = None
    if (agent / "trace.jsonl").exists():
        for line in (agent / "trace.jsonl").read_text().splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            kind = event.get("type")
            if kind == "llm_end" and not event.get("purpose"):
                calls += 1
            elif kind == "tool_start":
                tools[event.get("tool_name", "?")] += 1
            elif kind == "compact":
                compactions += 1
            elif kind == "run_end":
                status = event.get("status")
            elif kind == "system_prompt" and offered is None:
                offered = event.get("tools")
    rewards = (result.get("verifier_result") or {}).get("rewards") or {}
    exception = result.get("exception_info") or {}
    input_tokens = tokens.get("input", 0)
    cached = tokens.get("cached", 0)
    return {
        "trial": path.name,
        "task": path.name.split("__")[0],
        "reward": rewards.get("reward"),
        "cost_usd": tokens.get("cost_usd", 0.0),
        "input": input_tokens,
        "cached": cached,
        "uncached": input_tokens - cached,
        "output": tokens.get("output", 0),
        "reasoning": tokens.get("reasoning", 0),
        "model_calls": calls,
        "tool_calls": sum(tools.values()),
        "compactions": compactions,
        "agent_minutes": _minutes(result.get("agent_execution")),
        "tools_used": dict(tools.most_common()),
        "tools_offered": offered,
        "status": status,
        "exception": exception.get("exception_type"),
    }


def trials(job: Path) -> list[dict]:
    return [
        trial(p)
        for p in sorted(job.iterdir())
        if p.is_dir() and "__" in p.name and (p / "result.json").exists()
    ]


def _avg(rows: list[dict], field: str) -> float | None:
    values = [r[field] for r in rows if r[field] is not None]
    return mean(values) if values else None


def _fmt(value: float | None, field: str) -> str:
    if value is None:
        return "-"
    if field == "cost_usd":
        return f"${value:.2f}"
    if field in ("reward", "agent_minutes", "model_calls", "tool_calls", "compactions"):
        return f"{value:.1f}"
    return f"{value / 1000:.0f}K"


def main() -> None:
    args = sys.argv[1:]
    out = None
    if "--csv" in args:
        i = args.index("--csv")
        out = Path(args[i + 1])
        del args[i : i + 2]
    if len(args) != 2:
        sys.exit(__doc__)
    jobs = [job_dir(Path(a)) for a in args]
    # Name each arm after its jobs_dir (the folder holding the timestamped run).
    arms = {(job.parent.name if "__" in job.name else job.name): trials(job) for job in jobs}
    if len(arms) != 2:
        arms = {f"{job.parent.name}/{job.name}": trials(job) for job in jobs}
    names = list(arms)

    print("## Per task (mean over attempts)\n")
    print("| Task | Arm | n | " + " | ".join(SHOWN) + " | tools used |")
    print("|" + "---|" * (len(SHOWN) + 4))
    for task in sorted({r["task"] for rows in arms.values() for r in rows}):
        for arm in names:
            rows = [r for r in arms[arm] if r["task"] == task]
            if not rows:
                continue
            used: Counter[str] = Counter()
            for r in rows:
                used.update(r["tools_used"])
            cells = [_fmt(_avg(rows, f), f) for f in SHOWN]
            tools = ", ".join(f"{k} {v}" for k, v in used.most_common())
            print(f"| {task} | {arm} | {len(rows)} | " + " | ".join(cells) + f" | {tools} |")

    print("\n## Per arm\n")
    print(
        "| Arm | trials | passed | total cost | "
        + " | ".join(f"mean {f}" for f in SHOWN[2:])
        + " |"
    )
    print("|" + "---|" * (len(SHOWN) + 2))
    for arm in names:
        rows = arms[arm]
        rewards = [r["reward"] for r in rows if r["reward"] is not None]
        passed = f"{sum(rewards):g}/{len(rewards)}" if rewards else "-"
        total = sum(r["cost_usd"] for r in rows)
        cells = [_fmt(_avg(rows, f), f) for f in SHOWN[2:]]
        print(f"| {arm} | {len(rows)} | {passed} | ${total:.2f} | " + " | ".join(cells) + " |")

    problems = [
        f"- {arm} / {r['trial']}: status={r['status']} exception={r['exception']}"
        for arm in names
        for r in arms[arm]
        if r["exception"] or r["status"] not in (None, "completed")
    ]
    if problems:
        print("\n## Trials that stopped early or errored\n")
        print("\n".join(problems))

    offered = defaultdict(set)
    for arm in names:
        for r in arms[arm]:
            offered[arm].add(tuple(r["tools_offered"] or ("(not recorded)",)))
    print(
        "\nTools offered: " + "; ".join(f"{a}: {sorted(map(list, s))}" for a, s in offered.items())
    )

    if out:
        with out.open("w", newline="") as f:
            writer = csv.DictWriter(
                f, fieldnames=["arm", "trial", "task", *FIELDS, "status", "exception"]
            )
            writer.writeheader()
            for arm in names:
                for r in arms[arm]:
                    writer.writerow({"arm": arm, **{k: r[k] for k in writer.fieldnames if k in r}})
        print(f"Rows written to {out}")


if __name__ == "__main__":
    main()
