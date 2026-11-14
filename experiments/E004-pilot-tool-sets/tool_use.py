"""How the agent read and edited files, where the cost went, and which tests failed.

Usage, from the main checkout:
    python3 experiments/E004-pilot-tool-sets/tool_use.py jobs/pilot-bash-image jobs/pilot-full-tools

Bash commands are sorted by pattern matching, so the counts are approximate,
and one command can count under several methods.
"""

import json
import re
import sys
from collections import Counter
from pathlib import Path

# GPT-6.1 Sol standard prices, USD per million tokens.
PRICE = {"uncached": 2.0, "cached": 0.1, "output": 10.0}

PY_SCRIPT = re.compile(r"python3?\s+-\s*<<|python3?\s+-c")
PY_WRITES = re.compile(r"write_text|\.write\(|open\([^)]*['\"][wa]")
BASH_READ = {
    "sed -n / head / tail slice": re.compile(
        r"sed\s+-n\s+['\"]?\d+,\d+p|head\s+-n?\s*\d+|head\s+-c|tail\s+-n?\s*\d+|nl\s+-ba.*\|\s*sed"),
    "find / ls": re.compile(r"(^|[;&|]\s*)(find|ls|tree)\b"),
    "cat of a whole file": re.compile(r"(^|[;&|]\s*)cat\s+(?![>|<])[^>|<]*\S"),
    "grep / rg": re.compile(r"(^|[;&|]\s*)(grep|rg)\b"),
}
BASH_EDIT = {
    "heredoc (cat > file)": re.compile(r"cat\s*>\s*\S+\s*<<|cat\s*<<\S*\s*>\s*\S+"),
    "sed -i / perl -pi": re.compile(r"sed\s+-i|perl\s+-pi"),
}


def trials(job):
    for trace in sorted(Path(job).rglob("agent/trace.jsonl")):
        yield trace.parent.parent


def tool_calls(trial):
    calls, out = {}, {}
    for line in open(trial / "agent" / "trace.jsonl"):
        e = json.loads(line)
        if e.get("type") == "tool_start":
            calls[e["tool_call_id"]] = (e["tool_name"], e.get("tool_args") or {})
        elif e.get("type") == "message" and e.get("role") == "tool":
            out[e["tool_call_id"]] = len(e.get("content") or "")
    return [(name, args, out.get(cid, 0)) for cid, (name, args) in calls.items()]


def report(job):
    arm = Path(job).name
    read, edit, tools, out_chars = Counter(), Counter(), Counter(), Counter()
    tokens = Counter()
    print(f"\n## {arm}")
    for trial in trials(job):
        for name, args, n in tool_calls(trial):
            tools[name] += 1
            out_chars[name] += n
            if name != "bash":
                continue
            cmd = args.get("command", "")
            if PY_SCRIPT.search(cmd):
                if PY_WRITES.search(cmd):
                    edit["Python script that writes a file"] += 1
                else:
                    read["Python script that checks results"] += 1
            for label, pat in BASH_READ.items():
                read[label] += bool(pat.search(cmd))
            for label, pat in BASH_EDIT.items():
                edit[label] += bool(pat.search(cmd))
        t = json.load(open(trial / "agent" / "tokens.json"))
        tokens["uncached"] += t["input"] - t["cached"]
        tokens["cached"] += t["cached"]
        tokens["output"] += t["output"]
        tests = json.load(open(trial / "verifier" / "ctrf.json"))["results"]["tests"]
        failed = sorted(x["name"].split("::")[-1] for x in tests if x["status"] != "passed")
        print(f"  {trial.name:34} passed {len(tests) - len(failed)}/{len(tests)}  failed: {failed}")

    edit["edit tool"], edit["write tool"] = tools["edit"], tools["write"]
    read["read tool"] = tools["read"]
    print("  tool calls:", dict(tools.most_common()))
    print("  tool output, K characters:", {k: round(v / 1000) for k, v in out_chars.most_common()})
    print("  reading/inspecting:", {k: v for k, v in read.most_common() if v})
    print("  editing:", {k: v for k, v in edit.most_common() if v})
    cost = {k: tokens[k] * PRICE[k] / 1e6 for k in PRICE}
    total = sum(cost.values())
    print("  cost split:", {k: f"${v:.2f} ({v / total:.0%})" for k, v in cost.items()})


if __name__ == "__main__":
    for job in sys.argv[1:]:
        report(job)
