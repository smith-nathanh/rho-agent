# E006: Pilot, full tools at max reasoning effort, GPT-6.1 Sol

| | |
|---|---|
| Status | Running |
| Dates | started 2026-11-13 |
| Follows | [E004](../E004-pilot-tool-sets/), arm B (full tools, medium effort) |
| rho-agent commit | `1c528d9c82f999fa907285bdb7d0af119ac49f02` (same as E004) |
| Harbor version | 0.25.0 |
| Dataset | `terminal-bench/terminal-bench` 4.1.0 |
| Budget | worst case $80 (8 trials x $10 cap); expected $10–20 |
| Cost | — |
| Job output | `jobs/pilot-max-effort/` in the main checkout |

## Question

Does max reasoning effort fix the failures that repeated in every E004 trial,
and what does it cost in money and time? The Terminal-Bench 4.1 leaderboard
runs GPT-6.1 Sol at max effort, so E005 likely needs it too.

## Hypothesis

In E004 the same tests failed in every attempt, the agent always confirmed it
was done at the first ask of the completion gate, and no trial used more than
15 of its 120 minutes. If the failures come from stopping too early, more
reasoning should catch some of them. Output tokens were 56% of E004's cost and
model generation 90% of its time, so both should rise roughly with output,
perhaps 5–10 times.

## Design

- **Same as E004 arm B:** full default tool set, the same 4 tasks
  (`html-js-filter`, `bun-sourcemap-leak`, `payments-pipeline-fix`,
  `mvcc-lsm-compaction`), 2 attempts each, completion gate on (3 rounds max),
  same commit and system prompt.
- **Changed** ([`configs/pilot-max-effort.yaml`](configs/pilot-max-effort.yaml)):

  | Setting | E004 | E006 | Why |
  |---|---|---|---|
  | `reasoning_effort` | medium | max | the variable under test |
  | Wait for the next bytes of a response | 600 s | 1,800 s | long silent reasoning |
  | Longest model call (`max_call_seconds`) | 1,200 s | 3,600 s | E004's slowest call was 152 s at medium |
  | Cost cap per trial | $4 | $10 | E004's costliest trial was $0.85 at medium |
  | Time cap per trial | 2 h | 4 h | tasks themselves allow 8 h |
  | Trials at once | 2 (plus 2 from arm A) | 4 | same number of containers |

- **Baseline:** E004 arm B (3/8 passed, $2.21, 6.0 agent minutes per trial).

## Metrics

Compared with E004 arm B, per task and overall:

- **Primary:** which tests fail, and whether the failures that repeated in
  E004 go away; pass/fail.
- **Cost and time:** cost per trial; output and reasoning tokens; agent
  minutes split into model and tool time; longest model call.
- **Behaviour:** model and tool calls; completion-gate rounds.
- **Checks:** no trial stopped on a cap or a timeout, and the longer
  response wait reached the container.

## Limits

- 8 trials: a pass-rate change of one or two trials is not conclusive. The
  repeated failures are the sharper signal: in E004, 3 of 4 tasks failed the
  same tests in every failed attempt.
- Raising the caps means a trial that runs away costs more before it stops.

## Run

```bash
# from the main checkout
uv run --env-file .env harbor run --config jobs/pilot-max-effort.yaml
python3 experiments/tools/compare_trials.py jobs/pilot-full-tools jobs/pilot-max-effort --csv experiments/E006-pilot-max-effort/trials.csv
python3 experiments/E004-pilot-tool-sets/tool_use.py jobs/pilot-full-tools jobs/pilot-max-effort
```

## Results

—

## Cost

—
