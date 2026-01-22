# AgentBench Evaluation Module

This module runs [AgentBench](https://github.com/THUDM/AgentBench) tasks through ro-agent's harness to evaluate LLM performance on database queries (DBBench) and Linux system tasks (OS Interaction).

## Quick Start

```bash
# Run DBBench (database tasks)
ro-eval dbbench ~/proj/AgentBench/data/dbbench/standard.jsonl --model gpt-5-mini

# Run OS Interaction - full benchmark (156 tasks)
ro-eval os-interaction ~/proj/AgentBench/data/os_interaction --model gpt-5-mini

# Run OS Interaction - single file (26 tasks)
ro-eval os-interaction ~/proj/AgentBench/data/os_interaction/data/dev.json \
  --scripts ~/proj/AgentBench/data/os_interaction/scripts/dev \
  --model gpt-5-mini
```

## Commands

### `ro-eval dbbench`

Runs database query tasks. The agent must answer questions about data by executing SQL queries.

```bash
ro-eval dbbench DATA_FILE [OPTIONS]

Arguments:
  DATA_FILE              Path to standard.jsonl from AgentBench

Options:
  -m, --model TEXT       Model to use (default: gpt-5-mini)
  --base-url TEXT        API base URL for OpenAI-compatible endpoints
  --max-turns INTEGER    Max turns per task (default: 20)
  -p, --parallel INTEGER Run N tasks in parallel (default: 1)
  -o, --output TEXT      Output directory for results
  -n, --limit INTEGER    Only run first N tasks
  --offset INTEGER       Skip first N tasks
  --select-only          Only run SELECT queries (no Docker/MySQL needed)
  --system-prompt TEXT   Path to custom system prompt file
```

### `ro-eval os-interaction`

Runs Linux system tasks. The agent must solve problems by executing shell commands in a Docker container.

```bash
ro-eval os-interaction DATA_FILE [OPTIONS]

Arguments:
  DATA_FILE              Path to task JSON file (e.g., dev.json)

Options:
  -s, --scripts TEXT     Path to check scripts directory
  -m, --model TEXT       Model to use (default: gpt-5-mini)
  --base-url TEXT        API base URL for OpenAI-compatible endpoints
  --max-turns INTEGER    Max turns per task (default: 8)
  -p, --parallel INTEGER Run N tasks in parallel (default: 1)
  -o, --output TEXT      Output directory for results
  -n, --limit INTEGER    Only run first N tasks
  --offset INTEGER       Skip first N tasks
```

## Setup

### DBBench Setup

**For SELECT queries (most tasks):** No setup required. DBBench creates temporary SQLite databases automatically.

**For mutation queries (INSERT/UPDATE/DELETE):** Requires Docker with MySQL 8. A MySQL container is started automatically when needed.

```bash
# Pull the MySQL image (done automatically, but you can pre-pull)
docker pull mysql:8
```

The MySQL container uses tmpfs for fast ephemeral storage and is cleaned up after the evaluation completes.

### OS Interaction Setup

OS Interaction tasks run commands inside Docker containers. You need to build the AgentBench Docker images first.

**1. Make sure Docker is installed and running**

**2. Build the Docker images** (from the AgentBench repo):

```bash
cd ~/proj/AgentBench

docker build -t local-os/default -f ./data/os_interaction/res/dockerfiles/default data/os_interaction/res/dockerfiles
docker build -t local-os/packages -f ./data/os_interaction/res/dockerfiles/packages data/os_interaction/res/dockerfiles
docker build -t local-os/ubuntu -f ./data/os_interaction/res/dockerfiles/ubuntu data/os_interaction/res/dockerfiles
```

These are simple Ubuntu containers with common tools (python3, git, vim, curl, wget, etc.).

**3. Verify the images exist:**

```bash
docker images | grep local-os
```

You should see:
```
local-os/default    latest    ...
local-os/packages   latest    ...
local-os/ubuntu     latest    ...
```

**Note:** If `docker.1ms.run/ubuntu` doesn't work (it's a mirror), edit the Dockerfiles to use `ubuntu:latest` instead.

## How It Works

### DBBench Flow

1. **Task Loading**: Parses `standard.jsonl` to extract questions, table schemas, and expected answers
2. **Database Setup**: Creates a temporary SQLite database (SELECT queries) or MySQL database in Docker (mutation queries)
3. **Agent Execution**: Runs the agent with two tools:
   - `execute_sql`: Run any SQL query (SELECT, INSERT, UPDATE, DELETE)
   - `commit_final_answer`: Submit the final answer
4. **Evaluation**: Compares the submitted answer to ground truth using:
   - Float tolerance (±0.01) for numeric values
   - Set comparison for multiple values
   - Exact match for mutation query results
5. **Cleanup**: Deletes the temporary database

### OS Interaction Flow

1. **Task Loading**: Parses JSON task file with descriptions and evaluation configs
2. **Container Setup**: Starts a Docker container with the specified image
3. **Initialization**: Runs init scripts and starts background processes
4. **Agent Execution**: Runs the agent with three tools:
   - `bash_action`: Execute any shell command in the container
   - `answer_action`: Submit the final answer
   - `finish_action`: Mark task complete (for action-based tasks)
5. **Evaluation**: Checks the answer using:
   - Direct string match (`match` type)
   - Check scripts like `integer-match.py`, `string-match.py` (`check` type)
6. **Cleanup**: Stops and removes the Docker container

### System Prompts

The agent receives task-specific system prompts:

**DBBench prompt** instructs the agent to:
- Explore data with SELECT queries first
- Execute SQL to find answers
- Use `commit_final_answer` to submit

**OS prompt** instructs the agent to:
- Execute commands one at a time
- Provide exact, precise answers
- Use `answer_action` or `finish_action` to complete

Custom prompts can be provided via `--system-prompt path/to/prompt.md`.

## Output Format

Results are saved to `results/<model>/` by default (e.g., `results/gpt-5-mini/`).

Three files are created:
- `runs.jsonl` - Per-task results
- `overall.json` - Aggregate metrics
- `summary.txt` - Human-readable summary

### `runs.jsonl`

One JSON object per line with task results:

```json
{
  "index": 0,
  "status": "completed",
  "result": {
    "is_correct": true,
    "answer": "42",
    "ground_truth": ["42"],
    "std_sql": "SELECT COUNT(*) FROM users",
    "type": "SELECT"
  },
  "history": [
    {"role": "system", "content": "..."},
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "...", "tool_calls": [...]},
    {"role": "tool", "content": "..."}
  ],
  "time": {"timestamp": 1705849200, "str": "2024-01-21 15:00:00"}
}
```

### `overall.json`

Aggregate metrics:

```json
{
  "total": 300,
  "validation": {
    "completed": 0.85,
    "agent context limit": 0.03,
    "agent validation failed": 0.02,
    "agent invalid action": 0.01,
    "task limit reached": 0.05,
    "task error": 0.02,
    "unknown": 0.02,
    "average_history_length": 4.5,
    "max_history_length": 20,
    "min_history_length": 2
  },
  "custom": {
    "overall": {
      "total": 300,
      "pass": 255,
      "wrong": 45,
      "acc": 0.85
    }
  }
}
```

### `summary.txt`

Human-readable summary:

```
==================================================
Evaluation Results
==================================================
Total tasks:     300
Passed:          255
Failed:          45
Accuracy:        85.00%

Status Breakdown:
  Completed:           285
  Context limit:       5
  Validation failed:   3
  Invalid action:      2
  Turn limit reached:  3
  Task error:          2

History Length:
  Average: 4.5
  Min:     2
  Max:     20
==================================================
```

## Examples

### Run a quick smoke test

```bash
# First 10 DBBench tasks
ro-eval dbbench data/dbbench/standard.jsonl -n 10 -o smoke_test/

# First 5 OS tasks
ro-eval os-interaction data/os_interaction/data/dev.json -n 5 -o smoke_test/
```

### Run without Docker (SELECT queries only)

```bash
# Skip mutation tasks - no MySQL/Docker needed
ro-eval dbbench data/dbbench/standard.jsonl --select-only
```

### Run with a different model

```bash
# Use Claude via OpenRouter
ro-eval dbbench data.jsonl \
  --model anthropic/claude-3.5-sonnet \
  --base-url https://openrouter.ai/api/v1

# Use local Ollama
ro-eval dbbench data.jsonl \
  --model llama3.1 \
  --base-url http://localhost:11434/v1
```

### Parallel execution

```bash
# Run 4 DBBench tasks in parallel
ro-eval dbbench data.jsonl -p 4 -o results/

# Run 2 OS tasks in parallel (limited by Docker resources)
ro-eval os-interaction data.json -p 2 -o results/
```

### Resume from a specific task

```bash
# Skip first 100 tasks, run next 50
ro-eval dbbench data.jsonl --offset 100 -n 50 -o results/
```

## Architecture

```
ro_agent/eval/
├── cli.py                 # Typer CLI commands
├── runner.py              # EvalRunner orchestrates execution
├── config.py              # EvalConfig, TaskResult, EvalMetrics
├── output.py              # Result formatting
│
├── tasks/
│   ├── base.py            # BaseTask ABC
│   ├── dbbench.py         # DBBench task loader
│   └── os_interaction.py  # OS task loader
│
├── tools/
│   ├── submit_answer.py   # commit_final_answer, answer_action, finish_action
│   ├── unrestricted_sqlite.py  # SQL execution without read-only limits
│   └── docker_shell.py    # Shell execution in Docker container
│
├── evaluators/
│   ├── db_evaluator.py    # DBBench answer comparison (ported from AgentBench)
│   └── os_evaluator.py    # Check script execution
│
└── docker/
    └── container.py       # Docker container lifecycle
```

## Comparison with AgentBench

| Feature | AgentBench | ro-agent eval |
|---------|------------|---------------|
| Database backend | MySQL | SQLite (SELECT) / MySQL Docker (mutations) |
| Container runtime | Docker | Docker |
| Agent framework | Custom | ro-agent core |
| Parallelism | Multi-process | asyncio + semaphore |
| Output format | Compatible | Compatible |
| Check scripts | Python scripts | Built-in + script support |

## Tool Design: Eval vs ro-agent Tools

The eval module uses **separate tool implementations** rather than the main ro-agent tools. This is intentional—the tools serve different purposes and have different interfaces.

### What's Shared

Both eval and ro-agent tools share:

- **Core infrastructure**: `Agent`, `Session`, `ModelClient`, `ToolRegistry` from ro-agent core
- **Base classes**: `ToolHandler`, `ToolInvocation`, `ToolOutput` from `ro_agent.tools.base`
- **Utility functions**: `format_rows()`, `DEFAULT_ROW_LIMIT` from `ro_agent.tools.handlers.database`

### Key Divergences

| Aspect | ro-agent Tools | Eval Tools | Why |
|--------|---------------|------------|-----|
| **Tool name** | `sqlite`, `mysql` (db_type) | `execute_sql` | AgentBench expects this specific name |
| **Interface** | Multi-operation: `query`, `list_tables`, `describe`, `export_query` | Single operation: direct SQL execution | Benchmark tasks use direct SQL only |
| **Inheritance** | Extends `DatabaseHandler` base class | Standalone implementation | Simpler, benchmark-specific |
| **Read-only** | Enforced via URI mode + SQL pattern checking | Allows INSERT/UPDATE/DELETE | Mutation benchmark tasks require writes |
| **Approval** | `requires_approval = True` | `requires_approval = False` | No human in eval loop |

### Why the Divergence Exists

**1. AgentBench prescribes a specific tool interface**

The benchmark was designed with a single `execute_sql` tool that takes `{"sql": "..."}` and returns results. The agent interaction looks like:

```
execute_sql(sql="SELECT Notes FROM ...") → formatted results
commit_final_answer(answer="Women +60kg Bronze") → submitted
```

The main ro-agent handlers use a different interface: tool name `sqlite` with `{"operation": "query", "sql": "..."}`. Changing this would break benchmark compatibility with AgentBench's expected format.

**2. Different use cases, different security models**

ro-agent's main database handlers are designed for **human researchers** doing exploratory work:
- Read-only guarantees protect production databases
- Multi-operation interface (`describe`, `list_tables`) supports schema exploration
- Approval required for queries against sensitive data

Eval handlers are designed for **automated benchmarking**:
- No human in the loop, so no approval workflow
- Mutation queries are valid benchmark tasks
- Simpler interface matches benchmark expectations

**3. Shell tools have the same pattern**

| ro-agent | Eval |
|----------|------|
| `ShellHandler` with allowlist (~40 safe commands) | `DockerShellHandler` (sandboxed by container) |
| Blocks dangerous patterns (`>`, `rm`, etc.) | Any command allowed inside container |
| For safe inspection on real systems | For benchmark tasks in isolated environments |

### Adding New Evals

When adding new evaluation benchmarks:

1. **Check the expected tool interface** — most benchmarks have specific tool names and parameter formats
2. **Create eval-specific handlers** if the interface differs from ro-agent tools
3. **Reuse utilities** like `format_rows()` where the logic is genuinely shared
4. **Keep using ro-agent core** (`Agent`, `Session`, `ToolRegistry`) — the harness and model integration are the same