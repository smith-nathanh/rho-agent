---
title: Continuum
description: Continuity-first agent loop for PRD-to-code implementation.
order: 12
---

## What Continuum Is

Continuum is a PRD-to-code automation system. Given a product requirements document, it spawns agent sessions that implement the project across multiple context windows — using handoff documents to preserve continuity between sessions.

The core asset is not orchestration. It is **continuity under partial information and repeated handoffs**.

Coding-agent workflows tend to fail not from a single visible bug, but from error propagation: a weak early assumption becomes a plan, the plan becomes a sequence of tasks, and later sessions inherit the sequence but not the uncertainty. The project drifts from the PRD while appearing to make progress.

Continuum defends against this by:

- **Re-reading the PRD every session** — the PRD is the stable anchor, not a plan derived from it
- **Preserving reasoning context in handoffs** — not just what changed, but what was tried and failed, what assumptions are weak, and what to verify first
- **Letting the agent decide what to do** — no upfront task DAG, no fixed pipeline. The agent reads the PRD and the latest handoff, then chooses the next best action

## How It Works

Continuum runs a loop of agent sessions:

```
1. Read the PRD
2. Read the latest handoff from .rho-agent/handoffs/
3. Spawn an agent session (single role — no planner/worker/reviewer split)
4. Agent works: implements, runs checks, commits at logical checkpoints
5. When context budget fills → agent writes a handoff document and exits
6. Repeat until the agent signals PROJECT COMPLETE or max sessions reached
```

Each session gets the full PRD and the latest handoff as context. The agent decides what to work on — it might implement a feature, validate an assumption, investigate a failure, revert a bad approach, or rewrite the plan. "Next best action" is not always "next task in a list."

## Handoff Documents

Handoffs are the primary continuity mechanism. They live in `.rho-agent/handoffs/` (gitignored) and are numbered sequentially:

```
.rho-agent/handoffs/
  001-scaffold-api-routes.md
  002-implement-auth-flow.md
  003-fix-auth-tests.md
```

Each handoff answers:

1. **Objective** — What are we trying to achieve relative to the PRD?
2. **What I did** — Commits, test results, observations.
3. **What I tried that didn't work** — Failed approaches the next session should not repeat.
4. **Assumptions I made** — So the next session can verify them.
5. **What's weak** — Low-confidence assumptions.
6. **What to verify first** — The single most valuable check before continuing.
7. **Next options** — Plausible next paths (plural), not just one instruction.

The "failed approaches" and "what's weak" fields are the most important. They prevent the next session from blindly inheriting a bad direction.

## Usage

```bash
rho-agent continuum path/to/prd.md --working-dir ./my-project
```

### Options

| Option | Default | Description |
|--------|---------|-------------|
| `--working-dir`, `-d` | `.` | Project working directory |
| `--model`, `-m` | `gpt-5-mini` | Model to use |
| `--max-sessions` | `10` | Max sessions before pausing |
| `--budget-threshold` | `0.7` | Context budget threshold (0-1) |
| `--context-window` | `400000` | Context window size |
| `--test-cmd` | | Test command (e.g., `pytest`) |
| `--lint-cmd` | | Lint command |
| `--typecheck-cmd` | | Typecheck command |
| `--branch` | | Create and use a git branch |
| `--resume` | | Resume from saved state |
| `--state` | | Path to state JSON file |

### Resume

Continuum saves state to `~/.config/rho-agent/continuum/` after every session. If a run is paused (max sessions reached) or interrupted, resume it:

```bash
rho-agent continuum path/to/prd.md --resume
```

Or point to a specific state file:

```bash
rho-agent continuum path/to/prd.md --resume --state ~/.config/rho-agent/continuum/abc123.json
```

### Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Project completed |
| 1 | Error |
| 3 | Paused (max sessions reached, resumable) |

## Example

```bash
# Start a new project from a PRD
rho-agent continuum docs/prd.md \
  --working-dir ~/projects/my-app \
  --branch feature/initial-build \
  --test-cmd "pytest" \
  --max-sessions 5

# Check progress — handoffs are readable markdown
cat ~/projects/my-app/.rho-agent/handoffs/003-implement-auth.md

# Continue where it left off
rho-agent continuum docs/prd.md --resume
```

## Design

For the full design rationale, see [docs/design/continuum-v2.md](https://github.com/smith-nathanh/rho-agent/blob/main/docs/design/continuum-v2.md).
