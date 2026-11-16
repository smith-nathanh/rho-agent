# E007: Full Terminal-Bench 2.1, GPT-6.1 Sol, on Daytona

| | |
|---|---|
| Status | Done |
| Dates | setup check 2026-11-15; run 2026-11-15 22:26 to 2026-11-16 01:28; 3 setup failures rerun 2026-11-16 08:45 to 09:00 |
| rho-agent commit | `87bb9ac3b9f4fd332e6207b7018906f67e113f9b` (`main` after smith-nathanh/rho-agent#33) inside the sandbox; Harbor installer from `21a510a` (smith-nathanh/rho-agent#34) for the 3 reruns |
| Harbor version | 0.25.0; Daytona SDK 0.231.1 |
| Dataset | `terminal-bench/terminal-bench-2-1` (Harbor hub), all 89 tasks |
| Budget | model: worst case $356 (89 x $4 cap), expected $20–40; Daytona: a few dollars (small CPU sandboxes) |
| Score | **79 / 89 (88.8%)**. Flag: 2 of the 10 failures were refused by OpenAI's cybersecurity filter, not attempted (79 / 87 = 90.8% without them) |
| Cost | $10.23 model (full run incl. reruns) + $0.10 (setup check); Daytona billed separately |

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

## Design

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

## Results

Job: `jobs/e007-tb21/2026-11-15__22-26-05/` in the main checkout. Wall time
3 h 02 min for the main run, 15 minutes for the reruns.

**79 of 89 passed (88.8%).** With one attempt per task the standard error is
about 3 points. **Flag:** 2 of the 10 failures are tasks the OpenAI API
refused to work on ([below](#flag-2-tasks-blocked-by-openais-cybersecurity-filter));
without them the score is 79 of 87 (90.8%).

- **No timeouts.** No trial hit its agent time limit or the $4 cap. Agent
  time per trial: median 1.4 minutes, longest 33.5 minutes.
- **Cost:** $10.23 in model calls (32.1M input, 0.43M output tokens). Median
  $0.059 per trial, most $1.10 (`make-doom-for-mips`).
- **Sandbox refusals:** 37 `EnvironmentStartTimeoutError` retries in the main
  run's log, from the account's 10 GiB memory total. All but one trial
  eventually started (see reruns).

### Reruns of 3 setup failures

In the main run three trials ended before the agent started, so they had no
agent result and no cost:

| Task | Why it did not start |
|---|---|
| `gpt2-codegolf` | Asks for 8 GB; with two 2 GB trials beside it the account's 10 GiB total was exceeded, and all 11 start attempts were refused (`EnvironmentStartTimeoutError`) |
| `qemu-startup` | Harbor's setup installs `curl` and `git` with apt. The image is Debian 11 (bullseye), whose security packages Debian moved to `archive.debian.org` in 2026, so the install got 404s (`NonZeroAgentExitCodeError`) |
| `qemu-alpine-ssh` | Same Debian archive failure |

On 2026-11-16 they were rerun inside the same job, and their results replace
the setup failures in every figure on this page:

- **Fix:** rho-agent's Harbor installer now points bullseye's security source
  at `archive.debian.org` before installing anything
  (smith-nathanh/rho-agent#34, commit `21a510a`). It changes only the
  installer that Harbor runs on the Mac; the agent inside the sandbox is still
  `87bb9ac`. The qemu tasks' own test scripts also install packages with apt,
  so the fix is also what lets them be graded.
- **How:** `harbor job resume -p <job dir> -f NonZeroAgentExitCodeError -f
  EnvironmentStartTimeoutError`, with `PYTHONPATH` pointing at an export of
  `21a510a`. Harbor removed the three failed trials and reran them with the
  job's saved config (same model, settings and dataset version). With only
  these three left, `gpt2-codegolf` got its 8 GB.
- **Outcome:** all three passed: `gpt2-codegolf` $0.20, `qemu-alpine-ssh`
  $0.09, `qemu-startup` $0.07 ($0.36 in all).
- **Kept for reference:** the failed attempts and the job's `result.json`
  from before the rerun are in `jobs/e007-tb21/setup-failures/`.

### Trials that did not pass

| Task | Outcome | GPT-5.2 passes (of 5) |
|---|---|---|
| `model-extraction-relu-logits` | **Blocked by OpenAI's cyber filter** (see flag below) | 0 |
| `break-filter-js-from-html` | **Blocked by OpenAI's cyber filter** (see flag below) | 1 |
| `filter-js-from-html` | Failed: the filter changed all 12 clean HTML test files | 0 |
| `mteb-retrieve` | Failed: wrong answer (`HumanEval...` instead of `MTEB...`), as in E008 | 0 |
| `make-doom-for-mips` | Failed: expected `I_InitGraphics` line missing from the output | 0 |
| `install-windows-3.11` | Failed: QEMU ran a different image than `/app/isos/win311.img` | 0 |
| `raman-fitting` | Failed: 2D peak offset 991 against 1239 expected (other parameters within tolerance) | 0 |
| `configure-git-webserver` | Failed: web server returned HTTP 404 | 2 |
| `extract-elf` | Failed: 0% of expected values found (75% needed) | 2 |
| `headless-terminal` | Failed: interactive test found the terminal closed | 5 |

### Flag: 2 tasks blocked by OpenAI's cybersecurity filter

**Mention this whenever the E007 score is quoted.** Two tasks were not
attempted: the OpenAI API refused the model calls partway through. They are
counted as failures in the score. They are not rho-agent errors, and the score
says nothing about Sol's ability on them.

| Task | What the task asks | Calls answered, then refused | First refusal (UTC) | Cost |
|---|---|---|---|---|
| `model-extraction-relu-logits` | Recover a ReLU network's weights by querying `forward()` | 1, then 4 refused (after `cat /app/forward.py`) | 2026-11-16 03:45:50 | $0.0009 |
| `break-filter-js-from-html` | Write HTML that still triggers `alert()` after an XSS filter | 2, then 4 refused (after reading `filter.py` and the tests) | 2026-11-16 06:01:52 | $0.0021 |

- **The error, verbatim,** from the trace (`llm_error` events):

  > This content was flagged for possible cybersecurity risk. If this seems
  > wrong, try rephrasing your request. If you're doing authorized security
  > work that requires more cyber permissive safeguards, apply for Daybreak
  > access via https://platform.openai.com/settings/organization/status-and-access
  > before retrying.

- **What happened in the agent:** rho-agent classed the error as `transient`
  and retried the same request 4 times, then ended the run with status
  `error`. Harbor recorded no exception and the grader scored 0.
- **The other 87 tasks:** no refusals. `filter-js-from-html`, the closest
  in theme, was answered normally, as in E008.
- **Comparability:** OpenAI applies this filter to any agent that calls Sol
  with standard access. Other agents' published TB 2.1 scores may have used
  different access, or may count these tasks differently.
- **Not pursued (2026-11-16):** OpenAI's Daybreak program is the route to
  fewer refusals. For `gpt-6.1-sol` the docs say that needs **Daybreak
  Red**, which requires separate approval, SSO and enforced MFA on the API
  organization, per-project enablement, and identity verification. The
  program is aimed at authorized security work; the docs do not mention
  benchmark evaluation. Repeated flagged traffic can also get an
  organization's access temporarily revoked, so the two tasks were not rerun.
  Sources: [Daybreak in the Responses API](https://developers.openai.com/api/docs/guides/daybreak),
  [Cybersecurity checks](https://developers.openai.com/api/docs/guides/safety-checks/cybersecurity).

### Compared with GPT-5.2 on TB 2.0

GPT-5.2's five full TB 2.0 runs (2026-01-27 to 2026-02-05) scored 16, 22, 27,
45 and 49 of 89; see [E008](../E008-sol-on-gpt-5.2-failures/) for their
settings.

- **The 31 tasks GPT-5.2 failed in all five runs:** Sol passed 25 and failed
  6 (`filter-js-from-html`, `install-windows-3.11`, `make-doom-for-mips`,
  `model-extraction-relu-logits` (blocked by the API), `mteb-retrieve`,
  `raman-fitting`).
- **Tasks GPT-5.2's best run (49) passed but Sol did not:**
  `break-filter-js-from-html` (blocked by the API) and `headless-terminal`
  (GPT-5.2 passed it in all five runs).
- Sol passed 32 tasks that GPT-5.2's best run failed.
- The comparison mixes versions (TB 2.0 against 2.1, 28 tasks fixed), local
  Docker against Daytona, and agent versions, so it is not a controlled
  model comparison.

E008 ran 7 of these tasks with the same settings the day before. Here
`dna-insert` passed (failed in E008), and `filter-js-from-html` and
`mteb-retrieve` failed again; `regex-chess`, `sam-cell-seg`,
`torch-tensor-parallelism` and `video-processing` passed both times.

## Cost

$10.2256 in model calls for the full run: $9.8647 in the main run and
$0.3609 for the 3 reruns, summed from each trial's `agent_result.cost_usd`.
Setup check: $0.1043. Daytona compute is billed separately and not included:
89 sandboxes that ran (plus refused starts, which cost nothing), most for a
few minutes.

## Open decisions

None. The 2 tasks blocked by the cyber filter stay counted as failures and
flagged; Daybreak access was not pursued (2026-11-16).

## Follow-ups

- rho-agent classes the cyber-policy refusal as `transient` and retries it 4
  times. It is not transient, and repeated flagged requests count against the
  organization, so it should end the run on the first refusal.
- Merge smith-nathanh/rho-agent#34 so later runs on bullseye images install.
