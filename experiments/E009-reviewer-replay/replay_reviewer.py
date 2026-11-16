"""Replay rho-agent's reviewer on saved Harbor trials (E009).

Rebuilds the event trace the reviewer would have received from each trial's
agent/trace.jsonl, formats it with the runner's own format_event_trace, and
asks the model for a verdict with the current prompt (v1) and a candidate (v2).
No agent is run and nothing is changed in the trials.

    uv run --env-file .env python experiments/E009-reviewer-replay/replay_reviewer.py \
        JOB_DIR OUT.jsonl [--model gpt-6.1-sol] [--effort medium] [--samples 1]
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from rho_agent.client.model import ModelClient
from rho_agent.core.events import AgentEvent
from rho_agent.eval.harbor import runner
from rho_agent.prompts.loader import load_prompt
from rho_agent.prompts.renderer import prepare_prompt

HERE = Path(__file__).parent
PROMPTS = {
    "v1": (runner._REVIEWER_PROMPT, runner._REVIEWER_SYSTEM),
    "v2": (
        HERE / "prompts" / "eval_reviewer_v2.md",
        "You review whether an AI agent's work on a task will pass grading. Be concise and direct.",
    ),
}


def rebuild(trace_path: Path) -> tuple[str, list[AgentEvent]]:
    """Instruction and actor-phase events, as run_task collected them."""
    instruction, events = "", []
    names: dict[str, str] = {}
    for line in trace_path.read_text().splitlines():
        e = json.loads(line)
        kind = e.get("type")
        if kind == "run_start":
            instruction = e["prompt"]
        elif kind == "message" and e.get("role") == "assistant" and e.get("content"):
            events.append(AgentEvent(type="text", content=e["content"]))
        elif kind == "tool_start":
            names[e["tool_call_id"]] = e["tool_name"]
            events.append(
                AgentEvent(
                    type="tool_start",
                    tool_name=e["tool_name"],
                    tool_call_id=e["tool_call_id"],
                    tool_args=e.get("tool_args"),
                )
            )
        elif kind == "message" and e.get("role") == "tool":
            events.append(
                AgentEvent(
                    type="tool_end",
                    tool_name=names.get(e.get("tool_call_id"), "?"),
                    tool_call_id=e.get("tool_call_id"),
                    tool_result=e.get("content") or "",
                )
            )
    return instruction, events


def verdict(version: str, text: str) -> str:
    """APPROVED / REVISION_NEEDED as the runner would act on it."""
    if version == "v1":  # runner: approval only if the reply starts with APPROVED
        return "APPROVED" if text.strip().startswith("APPROVED") else "REVISION_NEEDED"
    last = [ln.strip() for ln in text.strip().splitlines() if ln.strip()][-1:]
    if last and last[0].startswith("VERDICT: APPROVED"):
        return "APPROVED"
    if last and last[0].startswith("VERDICT: REVISION_NEEDED"):
        return "REVISION_NEEDED"
    return "UNPARSED"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("job_dir", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--model", default="gpt-6.1-sol")
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--samples", type=int, default=1)
    args = ap.parse_args()

    client = ModelClient(model=args.model, reasoning_effort=args.effort, timeout=600.0)
    trials = sorted(p for p in args.job_dir.iterdir() if (p / "agent" / "trace.jsonl").exists())
    total = 0.0
    with args.out.open("a") as out:
        for trial in trials:
            result = json.loads((trial / "result.json").read_text())
            reward = ((result.get("verifier_result") or {}).get("rewards") or {}).get("reward")
            instruction, events = rebuild(trial / "agent" / "trace.jsonl")
            trace = runner.format_event_trace(events)
            for version, (prompt_path, system) in PROMPTS.items():
                content, _ = prepare_prompt(
                    load_prompt(prompt_path),
                    {"task_instruction": instruction, "agent_trace": trace},
                )
                for sample in range(args.samples):
                    text, usage = await client.complete(
                        [
                            {"role": "system", "content": system},
                            {"role": "user", "content": content},
                        ]
                    )
                    if not usage.get("output_tokens"):  # complete() returns errors as text
                        raise SystemExit(f"model call failed: {text[:300]}")
                    cost = usage.get("cost_usd") or 0.0
                    total += cost
                    row = {
                        "trial": trial.name,
                        "task": trial.name.rsplit("__", 1)[0],
                        "reward": reward,
                        "prompt": version,
                        "sample": sample,
                        "verdict": verdict(version, text),
                        "trace_chars": len(trace),
                        "input_tokens": usage.get("input_tokens"),
                        "output_tokens": usage.get("output_tokens"),
                        "cost_usd": cost,
                        "review": text,
                    }
                    out.write(json.dumps(row) + "\n")
                    out.flush()
                    print(
                        f"{row['task']:28} reward={reward} {version} -> {row['verdict']:16}"
                        f" ${cost:.4f}",
                        flush=True,
                    )
    print(f"total ${total:.4f}")


if __name__ == "__main__":
    asyncio.run(main())
