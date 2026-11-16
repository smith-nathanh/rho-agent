# E007: Full Terminal-Bench 2.1, GPT-6.1 Sol, on Daytona

| | |
|---|---|
| Status | Ready, not run |
| Dates | planned 2026-11-15 |
| rho-agent commit | `87bb9ac3b9f4fd332e6207b7018906f67e113f9b` planned (`main` after smith-nathanh/rho-agent#33) |
| Harbor version | 0.25.0; Daytona SDK 0.231.1 |
| Dataset | `terminal-bench/terminal-bench-2-1` (Harbor hub), all 89 tasks |
| Budget | model: worst case $356 (89 x $4 cap), expected $20–40; Daytona: a few dollars (small CPU sandboxes) |
| Cost | setup check $0.10 model (Daytona billed separately) |

## Why Terminal-Bench 2.1 instead of 4.1

E005 planned the full Terminal-Bench 4.1 run on Daytona. Its setup check found
that 8 of the 66 tasks cannot run within the account's per-sandbox limits
(4 CPUs, 512 GB of disk), and 3 tasks need an H100. Fixing that means a
higher Daytona tier, editing tasks, or moving to Modal. E005 is deferred; this
experiment runs a version that fits the account as it is.

Resource requests, from each task's `task.toml` (downloaded from the Harbor hub
on 2026-11-15):

| Dataset | Tasks | GPU | Multi-container | Over 4 CPUs | Max memory / disk | Agent time limit |
|---|---|---|---|---|---|---|
| `terminal-bench-2-1` | 89 | 0 | 0 | 0 | 8 GB / 10 GB | 900–12,000 s per task |
| `terminal-bench` 4.1 | 66 | 3 | 11 | 9 | 32 GB / 1 TB | 28,800 s |
| `terminal-bench-cpu-only` | 63 | 0 | 11 | 7 | 16 GB / 50 GB | 28,800 s |

- TB 2.1 is TB 2.0 with 28 tasks fixed: changed external dependencies, wrong
  resource settings and instructions that did not match their tests
  ([release notes](https://tbench.ai/news/terminal-bench-2-1)). The task
  `install-windows-3-11` is renamed `install-windows-3.11`.
- It has an official leaderboard and third-party runs with Terminus 2, Claude
  Code and Codex, so the score can be compared with other agents. Top scores
  are near 90%, so it separates strong agents less than 4.1 does.
- `terminal-bench-cpu-only` is 4.1 without the three GPU tasks (the other 63
  tasks are identical to 4.1), so it still has the CPU problem.

## Question

What does rho-agent score on Terminal-Bench 2.1 with GPT-6.1 Sol at medium
effort, and at what cost per task?

## Design (draft)

- **Model:** `openai/gpt-6.1-sol`, medium reasoning effort
  ([E006](../E006-pilot-max-effort/): max costs about 5 times as much with the
  same failing tests).
- **Tools:** full tool set ([E004](../E004-pilot-tool-sets/) arm B).
- **Settings:** completion gate on (`confirm_done_max: 3`), reviewer off
  (decided in [E009](../E009-reviewer-replay/#decision-2026-11-15)), $4 cost
  cap per task, same system prompt for every task.
- **Time limits:** the benchmark's own per-task agent limits (900–12,000 s).
  rho-agent's `max_wall_seconds` is one value for the whole job, so it is
  turned off (0) and Harbor's limit applies. Harbor grades a trial that hits
  its limit (`AgentTimeoutError`, not retried), and the cost is still
  recorded: E000's three timed-out trials all have `cost_usd`.
- **Repetitions:** one attempt per task. The score has a standard error of
  about 5 points.
- **Where it runs:** Daytona, as in E005: Harbor on the Mac, one sandbox per
  trial, rho-agent installed from git inside it. All 89 tasks are single
  container, so all use Harbor's Direct strategy; none uses a separate verifier
  sandbox.
- **Resuming:** `harbor job resume -p <job dir>` reruns only trials without a
  `result.json`.
- **Trials at once:** 3. The account stays on its current Daytona tier
  (decided 2026-11-15), whose totals are 10 CPUs, 10 GiB of memory and
  30 GiB of disk; every task asks for 10 GB of disk. When 4 and 8 GB tasks
  overlap, Daytona refuses the sandbox for the memory total; Harbor retries
  `EnvironmentStartTimeoutError` up to 10 times, 1–5 minutes apart. Refused
  sandboxes cost nothing. Expected wall time: several hours (one E008 task
  took 1.5–7 agent minutes; the 89 task limits add up to 42 hours).
- **Dataset version:** pinned to the hub content hash
  `sha256:7d7bdc1cbedad549fc1140404bd4dc45e5fd0ea7c4186773687d177ad3a0699a`,
  the version the setup check and E008 ran.
- **Config:** [`configs/tb21-full.yaml`](configs/tb21-full.yaml) (dry run:
  89 trials).

## Setup check: Daytona smoke test, 2026-11-15

Config: [`configs/tb21-smoke.yaml`](configs/tb21-smoke.yaml). GPT-6.1 Terra,
medium effort, full tools, rho-agent `87bb9ac`, 2 tasks at once, $1 and
20-minute caps. Job output: `jobs/tb21-smoke/` in the main checkout.

Job: `jobs/tb21-smoke/2026-11-15__20-25-55/`.

| Task | Covers | Outcome |
|---|---|---|
| `rstan-to-pystan` | largest request in TB 2.1 (4 CPU, 8 GB) | Passed (reward 1). Sandbox up in 12 s, agent 7 minutes, $0.10 |
| `filter-js-from-html` | the task whose sandbox failed in the E005 check | Sandbox not started within Harbor's 600 s (`EnvironmentStartTimeoutError`). No cost |

**Why the second sandbox did not start.** The two sandboxes together asked for
16 GB of memory. Creating them by hand with the Daytona SDK (same image and
resources, deleted straight after) gave:

> Failed to create sandbox: Total memory limit exceeded. Maximum allowed: 10GiB.

Harbor treats this as a transient error and retries after 60, 120, 180 and
240 s. `rstan-to-pystan` finished at about 7.5 minutes, and the next retry
would have come at 600 s, when the start timeout fired. Alone, the same
sandbox started in 20 s. The `html-js-filter` failure in the E005 check was
the same limit (four sandboxes at once).

**Account-wide limits** (Daytona tier at 2026-11-15, probed by creating small
sandboxes until refused; all deleted):

| Limit | Total across running sandboxes |
|---|---|
| CPUs | 10 |
| Memory | 10 GiB |
| Disk | 30 GiB |

Every TB 2.1 task asks for 10 GB of disk, so at most 3 trials fit at once,
fewer when 4 or 8 GB tasks overlap (memory: 68 tasks ask 2 GB, 13 ask 4 GB,
8 ask 8 GB). The sum of the 89 agent time limits is 42 hours.

Cost: $0.10 in model calls (Terra). Daytona compute is billed by Daytona and
not included; it was a few minutes of small sandboxes.

What it shows:

- TB 2.1 tasks run on Daytona with rho-agent as configured, and grade.
- The blocker is the account's total limits, not the per-sandbox ones.
  `n_concurrent_trials` must keep the total under 10 CPUs, 10 GiB and 30 GiB,
  or trials fail to start.

## Open decisions

None. Run it from the main checkout inside tmux (commands at the top of the
config); if it stops, `harbor job resume` reruns only unfinished trials.
