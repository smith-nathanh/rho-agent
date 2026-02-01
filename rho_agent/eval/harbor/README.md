# Harbor Integration

This directory contains the Harbor integration for running rho-agent on Harbor benchmarks (TerminalBench and others).

## What is TerminalBench?

TerminalBench is a benchmark for evaluating AI agents that operate exclusively through a terminal interface. The name refers to the **interface constraint**, not the task domain - all tasks must be solved using only command-line tools, with no GUI, browser, or IDE access.
This integration currently targets **TerminalBench-2** (tasks sourced from `laude-institute/terminal-bench-2`).

### What it tests

TerminalBench measures whether an AI agent can accomplish real-world tasks given only a bash shell. This requires:

- **Tool discovery**: Knowing (or figuring out) which CLI tools to use
- **Command composition**: Chaining multiple tools together (pipes, scripts, etc.)
- **Error diagnosis**: Reading and debugging text-based error messages
- **System knowledge**: Understanding how Linux, networking, compilers, etc. work
- **Resourcefulness**: Installing packages, reading docs, adapting when things fail

Even tasks involving computer vision or databases must be solved through terminal commands - installing packages via apt/pip, running scripts, parsing output, and writing results to files.

### Why terminal-only?

The terminal constraint creates a consistent, reproducible evaluation environment:
- No ambiguity about what the agent can "see" or "click"
- Easy to sandbox in Docker containers
- Matches how many real automation and DevOps tasks work
- Tests genuine problem-solving rather than UI navigation

## How Harbor Works

[Harbor](https://github.com/laude-institute/harbor) is a framework for running AI agent benchmarks in isolated Docker containers.

### Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Harbor Orchestrator                                        │
│  - Loads job config (job.yaml)                              │
│  - Downloads task definitions from registry                 │
│  - Spins up Docker containers per task                      │
│  - Runs agent, then verifier                                │
│  - Collects results                                         │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│  Docker Container (per task)                                │
│  ┌─────────────────────────────────────────────────────┐   │
│  │  Task Environment                                    │   │
│  │  - Pre-configured with task-specific files           │   │
│  │  - /app contains the working directory               │   │
│  │  - instruction.md describes what to do               │   │
│  └─────────────────────────────────────────────────────┘   │
│                              │                              │
│                              ▼                              │
│  ┌─────────────────────────────────────────────────────┐   │
│  │  Agent (rho-agent)                                    │   │
│  │  - Reads instruction                                 │   │
│  │  - Executes bash commands, writes files              │   │
│  │  - Works until task complete or timeout              │   │
│  └─────────────────────────────────────────────────────┘   │
│                              │                              │
│                              ▼                              │
│  ┌─────────────────────────────────────────────────────┐   │
│  │  Verifier                                            │   │
│  │  - Runs task-specific tests or checks                │   │
│  │  - Outputs reward (0.0 or 1.0)                       │   │
│  └─────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

### Execution flow

1. Harbor reads the job config and downloads tasks from the registry
2. For each task, Harbor starts a fresh Docker container with the task environment
3. The agent (`RhoAgent`) is installed and runs with the task instruction
4. The agent executes commands, writes files, installs packages as needed
5. When the agent finishes (or times out), Harbor runs the verifier
6. The verifier checks if the task was completed correctly (reward = 0 or 1)
7. Results are logged to `~/proj/harbor/jobs/<timestamp>/`

## Running Evaluations

### Prerequisites

```bash
# From the harbor directory
cd ~/proj/harbor

# Install rho-agent as editable dependency
uv pip install -e ~/proj/rho-agent
```

### Configuration files

Config files are in `configs/`:

| File | Dataset | Tasks | Use case |
|------|---------|-------|----------|
| `terminal-bench-prelim.yaml` | hello-world | 1 | Smoke test - verify setup works |
| `terminal-bench-sample.yaml` | terminal-bench-sample | 10 | Quick evaluation across task types |
| `terminal-bench.yaml` | terminal-bench | 89 | Full benchmark run |

Agent kwargs in these configs support:
- `enable_reviewer` / `reviewer_max_iterations`: post-execution review loop
- `enable_confirm_done` / `confirm_done_max`: require explicit `CONFIRM_DONE` before finishing

### Running

```bash
cd ~/proj/harbor

# Smoke test (1 task)
uv run harbor run --config ~/proj/rho-agent/rho_agent/eval/harbor/configs/terminal-bench-prelim.yaml

# Sample evaluation (10 tasks)
uv run harbor run --config ~/proj/rho-agent/rho_agent/eval/harbor/configs/terminal-bench-sample.yaml

# Full benchmark (89 tasks)
uv run harbor run --config ~/proj/rho-agent/rho_agent/eval/harbor/configs/terminal-bench.yaml
```

### Environment variables

| Variable | Description | Example |
|----------|-------------|---------|
| `RHO_AGENT_SERVICE_TIER` | OpenAI service tier (e.g. `flex` for 50% cost savings, slower) | `flex` |

```bash
# Run with flex service tier
RHO_AGENT_SERVICE_TIER=flex uv run harbor run --config ~/proj/rho-agent/rho_agent/eval/harbor/configs/terminal-bench.yaml
```

### Viewing results

Results are saved to `~/proj/harbor/jobs/<timestamp>/`:

```
jobs/2026-01-24__20-44-22/
├── config.json          # Job configuration
├── job.log              # Overall job log
├── result.json          # Aggregated results with reward stats
└── <task-name>__<id>/   # Per-task directory
    ├── trial.log        # Detailed execution log
    ├── result.json      # Task result with reward
    ├── agent/
    │   ├── agent_stdout.txt   # Agent's full output
    │   ├── agent_stderr.txt   # Any errors
    │   └── telemetry.db       # Full tool traces (SQLite)
    └── verifier/
        ├── reward.txt         # Score (0 or 1)
        └── test-stdout.txt    # Test output
```

Browse results visually:
```bash
cd ~/proj/harbor
uv run harbor view
```

### Debugging with telemetry

Each task captures a `telemetry.db` SQLite database with full tool traces (names, arguments, results, duration, success/failure). This is powered by rho-agent's observability system.

Query a task's traces directly:
```bash
# See all tool calls for a task
sqlite3 jobs/<timestamp>/<task>/agent/telemetry.db \
  "SELECT tool_name, arguments, result, duration_ms FROM tool_executions"
```

Or view all sessions in the Streamlit dashboard:
```bash
# Point the dashboard at a task's telemetry DB
rho-agent dashboard --db jobs/<timestamp>/<task>/agent/telemetry.db
```

## Terminal-Bench-Sample Tasks (10 tasks)

| Task | Category | Difficulty | Description |
|------|----------|------------|-------------|
| **log-summary-date-ranges** | data-processing | medium | Analyze log files, count ERROR/WARNING/INFO by date ranges, output CSV |
| **fix-code-vulnerability** | security | hard | Find and fix CWE vulnerabilities in the Bottle web framework |
| **qemu-startup** | sys-admin | medium | Start Alpine Linux in QEMU, expose telnet on port 6665 |
| **regex-log** | data-processing | medium | Write regex to extract dates from log lines containing IPv4 addresses |
| **polyglot-c-py** | software-eng | medium | Create a file that runs as both Python and C, computing Fibonacci |
| **build-cython-ext** | debugging | medium | Fix pyknotid Cython extensions to work with NumPy 2.3.0 |
| **sqlite-with-gcov** | sys-admin | medium | Compile SQLite with gcov instrumentation |
| **chess-best-move** | games | medium | Analyze chess board image, find best move using Stockfish |
| **configure-git-webserver** | sys-admin | hard | Set up git server with post-receive hook to deploy to web server |
| **qemu-alpine-ssh** | sys-admin | medium | Start QEMU Alpine VM with SSH server accessible on port 2222 |

### Task categories

- **System administration** (4): QEMU VMs, git server setup, SQLite compilation
- **Data processing** (2): Log analysis, regex pattern matching
- **Security** (1): Vulnerability detection and fixing
- **Software engineering** (1): Polyglot C/Python code
- **Debugging** (1): Cython/NumPy compatibility issues
- **Games** (1): Computer vision + chess engine integration

## rho-agent Harbor Components

### Files

| File | Purpose |
|------|---------|
| `agent.py` | Harbor `BaseAgent` wrapper - handles setup and execution |
| `runner.py` | Entry point that runs inside the container |
| `configs/*.yaml` | Job configurations for different benchmarks |

The agent uses rho-agent's capability profiles to configure tools. In eval mode, it has unrestricted access to:
- `bash` - Shell execution (container provides sandboxing)
- `write` - File creation/overwriting
- `edit` - Surgical file edits
- `read`, `grep`, `glob`, `list` - File inspection tools

### How rho-agent runs in Harbor

1. **Setup phase** (`agent.py:setup`):
   - Uploads rho-agent source to `/rho-agent` in container
   - Installs uv and syncs dependencies

2. **Run phase** (`agent.py:run`):
   - Executes `runner.py` with the task instruction
   - Runner creates an agent with unrestricted tools (bash, write_file, edit_file)
   - Agent works in `/app` directory until complete or timeout

3. **Verification**:
   - Harbor runs task-specific tests
   - Reward written to `verifier/reward.txt`

## Other Harbor Benchmarks

Beyond TerminalBench, these Harbor benchmarks are good fits for rho-agent's tool profile (bash, file I/O, databases, no browser/GUI).

### Strong fits

| Benchmark | Tasks | What it tests |
|-----------|-------|---------------|
| **CompileBench** | 15 | Real-world compilation challenges: dependency resolution, legacy code, cross-compilation |
| **BixBench-CLI** | 205 | Computational biology data analysis via CLI |
| **SWE-Bench Verified** | 500 | Human-validated Python bug-fixing in real repos |
| **SWE-Bench Pro** | 731 | Multi-language (Python, JS/TS, Go) bug-fixing |
| **AlgoTune** | 154 | Algorithm optimization across math, physics, crypto, graphs |
| **Spider2-DBT** | 68 | dbt data transformations with DuckDB (plays to the database tools) |

### Worth considering

| Benchmark | Tasks | What it tests |
|-----------|-------|---------------|
| **AIME** | 60 | Math olympiad problems (agent can use Python to compute) |
| **USACO** | 304 | Competitive programming (graph theory, DP, geometry) |
| **ReplicationBench** | ~100 | Reproducing astrophysics computational results from papers |
| **MLGym-Bench** | 12 | ML tasks (CV, RL, tabular) - pass/fail on beating baselines |
| **SWE-smith** | 60K+ | Synthetic SWE tasks (massive scale for statistical power) |
| **SWT-Bench** | 433 | Test generation - write tests that catch bugs |

List all available datasets:
```bash
cd ~/proj/harbor && uv run harbor datasets list
```
