# Conductor

The conductor turns a PRD (Product Requirements Document) into working code. It decomposes the PRD into a task DAG, then executes each task sequentially using autonomous worker agents — committing changes, running checks, and reviewing code along the way.

## How it works

```
PRD  →  Planner  →  Task DAG  →  [ Worker → Commit → Checks → Reviewer ] per task  →  Done
```

1. **Planning** — A readonly agent reads the PRD and project structure, then produces a task DAG with dependencies, acceptance criteria, and suggested verification commands.
2. **Worker** — A developer agent implements each task. It runs in multi-turn sessions and can hand off to a fresh context when nearing the token budget.
3. **Checks** — Automated verification (tests, linting, type checking) runs after each task. Failures trigger a retry loop where a fresh worker fixes the issues.
4. **Reviewer** — A fresh-context agent reviews the diff, fixes issues directly, and re-runs checks.
5. **Commit** — Each task gets its own git commit(s), keeping changes isolated and auditable.

State is saved to disk after every phase change, so a run can be interrupted and resumed.

## Quick start

```bash
rho-agent conduct path/to/prd.md
```

The conductor will plan the tasks, execute them one by one, and print a summary table with per-task status and cost.

## CLI options

```
rho-agent conduct <prd> [OPTIONS]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--working-dir`, `-d` | `.` | Project directory to work in |
| `--model`, `-m` | `$OPENAI_MODEL` or `gpt-5-mini` | Model to use for all agents |
| `--state` | auto-generated | Path to state JSON file |
| `--branch` | none | Create and work on a git branch |
| `--resume` | false | Resume from saved state |
| `--test-cmd` | auto-detected | Override test command |
| `--lint-cmd` | auto-detected | Override lint command |
| `--typecheck-cmd` | auto-detected | Override typecheck command |
| `--no-reviewer` | false | Disable the reviewer gate |
| `--context-window` | 400000 | Token context window size |
| `--budget-threshold` | 0.7 | Context budget fraction before handoff |
| `--max-worker-turns` | 3 | Max model turns per worker session |
| `--max-worker-sessions` | 3 | Max handoff sessions per task before pausing |
| `--max-task-attempts` | 3 | Max retries when checks fail |
| `--team-id` | none | Telemetry team ID |
| `--project-id` | none | Telemetry project ID |

## Writing a PRD

A PRD is a markdown file. It can optionally include YAML frontmatter to pre-define the task structure:

```markdown
---
name: "My Project"
tasks:
  - id: T1
    title: "Create base project structure"
    depends_on: []
  - id: T2
    title: "Implement core logic"
    depends_on: [T1]
  - id: T3
    title: "Add tests"
    depends_on: [T2]
---

# Product Requirements Document

## Context
What the project is and why it exists.

## Goals
- What the implementation should achieve.

## Non-Goals
- What is explicitly out of scope.

## Acceptance Criteria
- How to verify the work is complete.
```

If you omit the frontmatter, the planner agent will decompose the PRD into tasks automatically.

## Examples

Run the conductor on a PRD:

```bash
rho-agent conduct prd.md --working-dir ./my-project
```

Use a feature branch with custom verification:

```bash
rho-agent conduct prd.md \
  --branch feature/new-thing \
  --test-cmd "pytest tests/ -v" \
  --lint-cmd "ruff check ." \
  --typecheck-cmd "pyright"
```

Resume after an interruption or pause:

```bash
rho-agent conduct prd.md --resume
```

Resume from a specific state file:

```bash
rho-agent conduct prd.md --state ~/.config/rho-agent/conductor/abc123.json --resume
```

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | All tasks completed successfully |
| 1 | Unrecoverable error |
| 2 | Tasks failed, none ready to run |
| 3 | Paused — a task needs manual attention (resume with `--resume`) |

## State and resumption

Conductor state is saved to `~/.config/rho-agent/conductor/{run_id}.json` by default (or the path given via `--state`). State is written atomically after every phase change.

When resuming, any tasks left `in_progress` are reset to `pending` so they can be retried. The task DAG, commit SHAs, usage tracking, and handoff documents are all preserved.

## Task lifecycle

Each task goes through these stages:

```
PENDING  →  IN_PROGRESS  →  DONE
                ↓
             FAILED (after max retries)
```

A task is marked `DONE` only after passing both the automated checks gate and the reviewer gate (if enabled). Failed tasks block any downstream dependents.

## Budget-aware handoff

Workers monitor their token usage against `context_window * budget_threshold`. When the threshold is hit, the worker generates a structured handoff document and a fresh session picks up where it left off. This allows the conductor to handle tasks that exceed a single context window.

## Architecture

```
rho_agent/conductor/
├── cli.py          # CLI entry point (conduct command)
├── models.py       # Task, TaskDAG, ConductorConfig, ConductorState
├── scheduler.py    # Main orchestration loop
├── planner.py      # PRD → TaskDAG (readonly agent)
├── worker.py       # Task implementation (developer agent)
├── reviewer.py     # Code review + direct fixes (developer agent)
├── checks.py       # Test/lint/typecheck runner
├── git_ops.py      # Branch creation, commits, diffs
├── state.py        # Atomic JSON persistence
└── prompts.py      # System/user prompt templates
```

The planner runs with a `readonly` capability profile (no file modifications). Workers and reviewers run with a `developer` profile (full file editing and shell access). All agents run with `auto_approve=True` for autonomous operation.
