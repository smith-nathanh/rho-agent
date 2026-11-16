# E000: Terminal-Bench 2.0 sample, GPT-5.4, bash only

| | |
|---|---|
| Status | Done (recorded after the fact, from the job files) |
| Dates | run 2026-03-17 |
| rho-agent commit | not pinned: installed from the repository's default branch on that day |
| Dataset | `terminal-bench-sample` 2.0 (8 tasks, `qemu-*` excluded) |
| Cost | $5.39 ($5.38 for the sample, $0.01 for two hello-world checks) |
| Job output | `jobs/rho-agent-terminal-bench/2026-03-17__16-51-13/` (sample), `2026-03-17__16-36-06/` and `16-40-40/` (hello-world) |

## Question

First end-to-end run of rho-agent on Terminal-Bench through Harbor: does it
work, and roughly how does bash-only rho-agent score?

## Design

- **Model:** `openai/gpt-5.4`, reasoning effort high, bash only.
- **Settings:** no completion gate, $2 cost cap per task, no reviewer.
- **Tasks:** the 8-task Terminal-Bench 2.0 sample, one attempt each.
- Before it, two `hello-world` runs with `openai/gpt-5.2` checked the pipeline
  (the first failed, the second passed).

## Results

3 of 8 passed (0.375). Three failures were agent timeouts.

| Task | Reward | Cost | Note |
|---|---|---|---|
| fix-code-vulnerability | 1 | $0.28 | |
| log-summary-date-ranges | 1 | $0.19 | |
| regex-log | 1 | $0.47 | |
| build-cython-ext | 0 | $1.83 | 4.5M input tokens |
| chess-best-move | 0 | $1.26 | agent timeout |
| configure-git-webserver | 0 | $0.22 | agent timeout |
| polyglot-c-py | 0 | $0.85 | agent timeout |
| sqlite-with-gcov | 0 | $0.27 | |

## Limits

Different model, dataset version and harness code from the November runs, so
not comparable with them. One attempt per task.

## Follow-ups

Led to the November harness work: completion gate, cost and time caps,
compaction settings, and the Harbor 0.25 port.
