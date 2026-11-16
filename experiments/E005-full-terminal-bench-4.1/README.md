# E005: Full Terminal-Bench 4.1, GPT-6.1 Sol

| | |
|---|---|
| Status | Deferred: Daytona limits. Terminal-Bench 2.1 runs first as [E007](../E007-full-terminal-bench-2.1/) |
| rho-agent commit | `87bb9ac3b9f4fd332e6207b7018906f67e113f9b` planned (`main` after smith-nathanh/rho-agent#33) |
| Harbor version | 0.25.0; Daytona SDK 0.231.1 |
| Dataset | `terminal-bench/terminal-bench` 4.1.0, all 66 tasks |
| Budget | model: worst case $264 (66 x $4 cap), expected $30–60; Daytona: expected $25–50 |
| Cost | setup check $0.56 model (Daytona billed separately) |

## Question

What does rho-agent score on Terminal-Bench 4.1 with GPT-6.1 Sol, and at what
cost per task?

## Design (draft)

- **Model:** `openai/gpt-6.1-sol` at medium reasoning effort. Max effort costs
  about 5 times as much and did not change which tests fail
  ([E006](../E006-pilot-max-effort/)). The leaderboard's Sol entry uses max, so
  the score is not directly comparable to it.
- **Tools:** full tool set ([E004](../E004-pilot-tool-sets/) arm B).
- **Settings:** completion gate on, $4 cost cap, a cap on agent time per trial
  (about 2 hours, which also bounds GPU cost), same system prompt for every task.
- **Repetitions:** one attempt per task (budget). The score then has a standard
  error of about 6 points.
- **Where it runs:** Daytona sandboxes, all 66 tasks (next section).
- Start from `rho_agent/eval/harbor/configs/terminal-bench.yaml`, pin
  `version` to a commit, and copy the final config into `configs/`.

## Execution environment: Daytona

### Why not local Docker

- **GPUs.** Three tasks need one NVIDIA H100: `fp8-rmsnorm-gemm`,
  `jax-speedrun-gpu` and `math-eval-grader`. The Mac has none. The Terminal-Bench
  run guide says to use a sandbox with GPU access and names Modal and Daytona
  ([tbench.ai/run](https://www.tbench.ai/run)); Vals.ai ran Terminal-Bench 4 on
  Daytona, GPU tasks included.
- **Architecture.** The task images are `amd64`; the Mac is `arm64`, so local
  runs are emulated. This can matter for tasks with timing thresholds. E000–E006
  all ran locally under emulation.
- **Memory.** Docker Desktop has 23.4 GiB; `jax-speedrun-gpu` asks for 32 GB.

### How it runs

- Harbor runs on the Mac (`environment.type: daytona`) and creates one Daytona
  sandbox per trial through the Daytona API. Nothing runs in local Docker.
- rho-agent is a Harbor installed agent: Harbor installs it from git inside the
  sandbox and runs the runner there, so model calls and tools both run in the
  sandbox. This is the same arrangement as Codex, Claude Code and
  mini-swe-agent in Harbor. rho-agent's own `--backend daytona` mode (agent
  local, tools remote) is not used.
- Harbor picks a strategy per task:
  - **Direct:** single-container tasks run the task image as the sandbox. GPU
    tasks must use this strategy.
  - **DinD:** multi-container tasks (11 of 66) run `docker:28.3.3-dind` as the
    sandbox and `docker compose` inside it.
- Some tasks grade in a separate verifier environment (`environment_mode =
  "separate"`), which is a second sandbox with its own resource request.
- GPU sandboxes must be ephemeral; Harbor's defaults (auto-delete on stop,
  `delete: true`) satisfy this. Harbor maps the task's `gpu_types = ["H100"]` to
  Daytona's `H100`.
- Credentials: `DAYTONA_API_KEY` and `DAYTONA_API_URL` from the main checkout's
  `.env`, passed with `uv run --env-file .env`.
- Account: billing added on 2026-11-14 with a $100 credit. Tier and limits:
  to be recorded before the run.

### Resource requests above Daytona's per-sandbox limits

Seen in the setup check: at most 4 CPUs and 512 GB of disk per sandbox on the
account's tier at the time. GPU sandboxes were allowed 8 CPUs. Tasks whose
environment or verifier asks for more than 4 CPUs:

| Task | CPUs (env / verifier) | Memory | Disk | GPU |
|---|---|---|---|---|
| `jax-speedrun-gpu` | 16 / 16 | 32 GB | **1,000 GB** | H100 |
| `live-database-cutover` | 16 / 16 | 16 GB | 20 / 40 GB | — |
| `math-eval-grader` | 8 / 8 | 16 GB | 30 GB | H100 (ran) |
| `distributed-dedup` | 8 / 8 | 16 GB | 15 GB | — |
| `formal-crypto` | 8 / 8 | 4 GB | 10 GB | — |
| `shadow-relay` | 8 / 8 | 4 GB | 10 GB | — |
| `wdm-design` | 8 / 8 | 16 GB | 10 GB | — |
| `lake-temp-glm` | 8 / — | 8 GB | 20 GB | — |
| `payments-pipeline-fix` | 4 / **8** | 8 / 16 GB | 10 GB | — |

Every other task asks for at most 4 CPUs and 50 GB of disk (47 of 66 ask for
10 GB).

### Changes to the benchmark

None yet. Planned, if the account cannot be raised:

- `jax-speedrun-gpu`: a local copy of the task with `storage_mb` lowered from
  1,024,000 to a value inside the limits (100 GB is plenty: about 2 GB of data
  plus the image and checkpoints). Run in the same job as the registry dataset
  (`exclude_task_names` plus a local `tasks` entry). The copy stays out of git,
  since task folders include solutions and tests.
- Harbor's `override_storage_mb` and `override_cpus` are not used: they apply to
  every task in a job.

## Setup check: Daytona smoke test, 2026-11-14

Config: [`configs/daytona-smoke.yaml`](configs/daytona-smoke.yaml). GPT-6.1
Terra, medium effort, full tools, rho-agent `87bb9ac`, 4 tasks at once, $1 and
20-minute caps. Job output: `jobs/daytona-smoke/2026-11-14__10-35-47/` in the
main checkout.

| Task | Covers | Outcome |
|---|---|---|
| `math-eval-grader` | GPU task (H100) | Ran end to end: sandbox up in 33 s, agent ran to its 20-minute cap, graded (reward 0). $0.27 |
| `payments-pipeline-fix` | multi-container (DinD) | Agent ran 10 minutes in the DinD sandbox ($0.29); grading failed: verifier sandbox asks for 8 CPUs, limit 4 |
| `jax-speedrun-gpu` | largest GPU task | Sandbox refused: 1,000 GB disk requested, limit 512 GB. No cost |
| `html-js-filter` | plain single-container task | Sandbox did not start within Harbor's 600 s. Cause, found in E007: the account's total memory limit (10 GiB) with the other sandboxes running |

What it shows:

- rho-agent installs and runs inside Daytona sandboxes, Direct and DinD, and
  GPU sandboxes work.
- At these limits, 8 of the 66 tasks cannot run as configured: every row of
  the table above except `math-eval-grader`. The per-sandbox memory limit was
  not hit and is not known.
- No Harbor sandboxes were left behind after the job. (One archived 1-CPU
  sandbox without Harbor labels predates this run.)

Cost: $0.56 in model calls (Terra). Daytona compute, mostly the H100 sandbox for
about 21 minutes, is billed by Daytona and not included.

## Open decisions

- **Daytona limits.** Raise the per-sandbox limits (a higher tier, or Daytona
  support) to at least 16 CPUs, 32 GB of memory and 1 TB of disk, or change
  the affected tasks. Changing CPU counts on 8 tasks alters the benchmark more
  than lowering one task's disk.
- **Trials at once.** Set from the account's total CPU, memory and disk limits.
- **Retries.** Use Harbor's `retry` setting for transient sandbox errors, and
  `harbor job resume` if the job stops.
- Whether to raise the request timeout (E006 saw a 618 s call at max; medium
  calls were far shorter).
