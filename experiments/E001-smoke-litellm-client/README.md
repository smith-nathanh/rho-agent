# E001: Smoke test, GPT-6.1 Sol, LiteLLM client

| | |
|---|---|
| Status | Done |
| Dates | run 2026-11-11 |
| rho-agent commit | branch `fix/core-loop` on that day (later merged as smith-nathanh/rho-agent#16) |
| Harbor version | 0.25.0 |
| Dataset | `terminal-bench/terminal-bench` 4.1.0, task `html-js-filter` |
| Cost | $0.20 |
| Job output | `jobs/rho-agent-smoke/2026-11-11__16-05-40/` |

## Question

Does the rebuilt harness (new session loop, Harbor 0.25 adapter, ATIF
trajectories, OpenAI and LiteLLM upgrades) run end to end against a real model?

## Design

- **Model:** `openai/gpt-6.1-sol`, reasoning effort medium.
- **Tools:** bash only.
- **Settings:** completion gate on (max 3 rounds), $2 cost cap, 40-minute time cap.
- **Task:** `html-js-filter`, the shortest task by expert estimate (45 minutes). One attempt.
- **Client:** `LiteLLMClient`. LiteLLM quietly sent Sol's calls to the
  Responses API, but the model's reasoning was **not** carried between calls
  (found later; fixed in smith-nathanh/rho-agent#29).

## Results

Passed (reward 1.0).

| Metric | Value |
|---|---|
| Agent time | about 7 minutes (15 for the whole trial) |
| Model calls / tool calls | 17 / 15 (all bash) |
| Input tokens | 159K (143K cached, 90%) |
| Output tokens | 15K (7.4K reasoning) |
| Completion gate | fired once; the agent confirmed |
| Trajectory | valid ATIF; totals matched `tokens.json` and Harbor |

## Limits

One task, one attempt. Did not exercise compaction, retries, the caps or the
per-call time limit.
