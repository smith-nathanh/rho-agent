# E004: Pilot, bash + images vs the full tool set, GPT-6.1 Sol

| | |
|---|---|
| Status | Ready, not run |
| Dates | planned 2026-11-13 |
| rho-agent commit | `1c528d9c82f999fa907285bdb7d0af119ac49f02` (`main` after smith-nathanh/rho-agent#31) |
| Harbor version | 0.25.0 |
| Dataset | `terminal-bench/terminal-bench` 4.1.0 |
| Budget | worst case $64 (16 trials x $4 cap); expected $10–25 |
| Cost | — |
| Job output | `jobs/pilot-bash-image/` and `jobs/pilot-full-tools/` in the main checkout |

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
- **Execution:** run the arms one after the other, 2 trials at a time
  (Docker Desktop has 7.7 GiB; some tasks ask for 4 GB each).

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
uv run --env-file .env harbor run --config jobs/pilot-bash-image.yaml
uv run --env-file .env harbor run --config jobs/pilot-full-tools.yaml
python3 experiments/tools/compare_trials.py jobs/pilot-bash-image jobs/pilot-full-tools --csv experiments/E004-pilot-tool-sets/trials.csv
```

## Results

Not run yet.

## Cost

Not run yet.

## Follow-ups

The winning tool set (or the cheaper one, if pass rates look the same) goes
into E005.
