# E004: Pilot, bash + images vs the full tool set, GPT-6.1 Sol

| | |
|---|---|
| Status | Done |
| Dates | run 2026-11-13, 17:14–17:56 local (both arms at once) |
| rho-agent commit | `1c528d9c82f999fa907285bdb7d0af119ac49f02` (`main` after smith-nathanh/rho-agent#31) |
| Harbor version | 0.25.0 |
| Dataset | `terminal-bench/terminal-bench` 4.1.0 |
| Budget | worst case $64 (16 trials x $4 cap); expected $10–25 |
| Cost | $4.36 ($2.15 arm A, $2.21 arm B) |
| Job output | `jobs/pilot-bash-image/2026-11-13__17-13-52/` and `jobs/pilot-full-tools/2026-11-13__17-14-12/` in the main checkout |

## Question

Before spending on the full Terminal-Bench run (E005), which tool set should
rho-agent use with GPT-6.1 Sol: bash alone (plus image viewing), or the full
default set of file tools? Mainly a question of token efficiency and cost.

## Hypothesis

Two plausible directions:

- **Full tools cost less:** `read`, `grep` and `edit` return bounded,
  structured output, where bash may `cat` whole files and rewrite them with
  heredocs.
- **Bash costs less:** smaller tool definitions on every call, and current
  models are fluent shell users. Observed in E001/E002: the agent used `sed`,
  `head` and `rg` to look at slices of files rather than dumping them.

The prior expectation is a small difference in tokens and no detectable
difference in pass rate at this sample size.

## Design

- **Arms** (`configs/`): the two configs are identical except for the tools.

  | Arm | Config | Tools offered |
  |---|---|---|
  | A: bash + images | `pilot-bash-image.yaml` | `bash`, `view_image` |
  | B: full tools | `pilot-full-tools.yaml` | `bash`, `edit`, `glob`, `grep`, `list`, `read`, `view_image`, `write` |

  Both arms have image viewing, so the only difference is the file tools.
  Subagents (`delegate`) are off in both.
- **Shared settings:** `openai/gpt-6.1-sol`, reasoning effort medium,
  completion gate on (3 rounds max), $4 cost cap, 2-hour time cap and
  20-minute per-call limit per trial. Same system prompt for every task
  (`rho_agent/prompts/eval_terminal.md`; task `AGENTS.md` files ignored).
- **Tasks** (chosen from names and metadata, short to mid-length, involving
  reading and editing existing code, where file tools should matter most):

  | Task | Why |
  |---|---|
  | `html-js-filter` | short (45 min expert); has E001/E002 baselines |
  | `bun-sourcemap-leak` | short (1.5–2 h); debugging existing code |
  | `payments-pipeline-fix` | short (1.5–2 h); fixing an existing pipeline |
  | `mvcc-lsm-compaction` | mid-length (4 h); navigating and editing a storage engine |

- **Repetitions:** 2 attempts per task per arm, so 8 trials per arm, 16 in all.
- **Execution:** both arms run at the same time, 2 trials each (4 containers
  at once; Docker Desktop has 23.4 GiB and some tasks ask for 4 GB), so both
  arms see the same API conditions.

## Metrics

Per task and per arm, using `tools/compare_trials.py`:

- **Primary:** cost; uncached input, cached input, output and reasoning tokens.
- **Secondary:** model calls, tool calls by tool, agent minutes, compactions.
- **Reported, not concluded on:** pass/fail.
- **Checks:** the tools offered (from the trace's `system_prompt` event) match
  the arm, and no trial errored or stopped on a cap for reasons unrelated to
  the task.

Read the result as a direction that is consistent across tasks. A difference
under roughly 20–30% on a single task can be run-to-run noise.

## Limits

- 4 tasks, 2 attempts: says little about pass rate (one flip on one task moves
  an arm's pass rate by 12.5 points).
- Short and mid-length tasks only. Long tasks, where compaction matters, are
  not covered.
- Tasks picked by name and metadata, not by reading them.

## Run

```bash
# from the main checkout, both at once
uv run --env-file .env harbor run --config jobs/pilot-bash-image.yaml &
uv run --env-file .env harbor run --config jobs/pilot-full-tools.yaml &
python3 experiments/tools/compare_trials.py jobs/pilot-bash-image jobs/pilot-full-tools --csv experiments/E004-pilot-tool-sets/trials.csv
```

## Results

All 16 trials completed and were graded; none errored or hit a cap. Every
trial ended with the agent confirming at the completion gate. The tools offered,
read from each trace, matched the arm. Per-trial rows are in
[`trials.csv`](trials.csv).

### Per arm

| Arm | Passed | Cost | Uncached input | Cached input | Output | Reasoning | Model calls | Tool calls | Agent minutes |
|---|---|---|---|---|---|---|---|---|---|
| A: bash + images | 1/8 | $2.15 | 28K | 506K | 16K | 7K | 22.1 | 20.1 | 5.8 |
| B: full tools | 3/8 | $2.21 | 26K | 684K | 16K | 7K | 29.9 | 29.8 | 6.0 |

Token and call figures are means per trial.

### Per task (mean of 2 attempts)

| Task | Arm | Passed | Cost | Model calls | Tools used (total over both attempts) |
|---|---|---|---|---|---|
| bun-sourcemap-leak | A | 0/2 | $0.10 | 13.0 | bash 22 |
| | B | 0/2 | $0.11 | 15.0 | bash 18, write 6, edit 2 |
| html-js-filter | A | 0/2 | $0.28 | 18.0 | bash 32 |
| | B | 1/2 | $0.28 | 25.5 | bash 26, edit 14, write 6, read 1 |
| mvcc-lsm-compaction | A | 0/2 | $0.08 | 10.5 | bash 17 |
| | B | 1/2 | $0.09 | 14.5 | bash 15, edit 7, list 3, read 3, write 2 |
| payments-pipeline-fix | A | 1/2 | $0.62 | 47.0 | bash 90 |
| | B | 1/2 | $0.63 | 64.5 | bash 54, edit 53, read 12, write 12, list 4 |

### What it shows

- **Cost is the same.** $2.15 vs $2.21 overall (3% apart) and within a few
  cents on every task. Uncached input, output and reasoning tokens are
  essentially equal.
- **Full tools take more, smaller steps.** About 35% more model calls and 48%
  more tool calls, on every task. The extra calls re-read the conversation
  from the prompt cache, which is why cached input is 35% higher; at $0.10 per
  million cached tokens that barely moves the cost. Time is the same.
- **With full tools, Sol uses `edit` and `write`, but still reads with bash.**
  `read` was used 16 times against 113 bash calls in arm B, and `grep` and
  `glob` never. So the expected saving from bounded file reads did not happen.
- **Pass rate: 3/8 vs 1/8, not distinguishable.** Fisher's exact test gives
  p = 0.57; the difference is two trials, on two tasks.
- **Runs were short and failures often narrow.** Mean agent time was about 6
  minutes against a 2-hour limit, on tasks rated at 45 minutes to 4 hours of
  expert time. Both `bun-sourcemap-leak` failures in arm A met 35 of 37
  criteria; the failed `mvcc-lsm-compaction` trial in arm B passed 11 of 15
  tests. Sol at medium effort finishes quickly and confidently, sometimes too
  soon.

## Cost

$4.36 in all: arm A $2.1493, arm B $2.2060, from each job's `result.json`
(which matches the sum over trials). Well under the $64 cap and the $10–25
expected.

## Follow-ups

- **Tool set for E005:** cost gives no reason to prefer either. Full tools is
  the default users get and was directionally ahead on pass rate, so it is the
  natural choice. Since a trial averaged $0.27, running E005 with both arms
  (66 tasks each) is affordable and would answer the pass-rate question with
  66 paired tasks instead of 4.
- **Reasoning effort:** the quick, near-miss failures suggest testing `high`
  effort, which may trade cost for more thorough verification.
