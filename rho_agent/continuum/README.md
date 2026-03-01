# Continuum

Continuum turns a PRD into working code, built around **continuity under partial information and repeated handoffs** rather than a fixed task pipeline.

The core problem: coding-agent workflows fail less from a single visible bug and more from error propagation. A weak early assumption becomes a plan, the plan becomes a sequence of tasks, later sessions inherit the sequence but not the uncertainty, and the project drifts from the PRD while appearing to make progress.

Continuum defends against this by re-reading the PRD every session, preserving reasoning context (not just diffs) across handoffs, and letting the agent decide what to do next rather than following a predetermined task DAG.

## How it works

```
PRD + Latest Handoff  →  Agent Session  →  Commit + Handoff  →  Repeat
```

1. **Session startup** — The agent reads the full PRD and the latest handoff document. It inspects project state (git log, tests, key files) and chooses what to work on next.
2. **Implementation** — A single agent does everything: implements, runs checks, commits at logical checkpoints. No planner/worker/reviewer split.
3. **Handoff** — When the context budget fills, the agent commits any uncommitted work, writes a handoff document preserving what it tried, what failed, what assumptions are weak, and what to verify first. Then it exits.
4. **Next session** — A fresh session picks up from the PRD + latest handoff. It decides the next best action — which might be continuing implementation, validating an assumption, reverting a bad approach, or rewriting the plan.
5. **Completion** — When the agent determines all PRD requirements are met and verified, it signals `PROJECT COMPLETE`.

Handoff documents live in `.rho-agent/handoffs/` (gitignored). They are numbered sequentially across the project, giving a readable timeline:

```
.rho-agent/handoffs/
  001-scaffold-api-routes.md
  002-implement-auth-flow.md
  003-fix-auth-tests.md
```

## Quick start

```bash
rho-agent continuum path/to/prd.md
```

## CLI options

```
rho-agent continuum <prd> [OPTIONS]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--working-dir`, `-d` | `.` | Project directory to work in |
| `--model`, `-m` | `$OPENAI_MODEL` or `gpt-5-mini` | Model to use |
| `--max-sessions` | 10 | Max sessions before pausing |
| `--budget-threshold` | 0.7 | Context budget fraction before handoff |
| `--context-window` | 400000 | Token context window size |
| `--test-cmd` | none | Test command (e.g., `pytest`) |
| `--lint-cmd` | none | Lint command |
| `--typecheck-cmd` | none | Typecheck command |
| `--branch` | none | Create and work on a git branch |
| `--resume` | false | Resume from saved state |
| `--state` | auto-generated | Path to state JSON file |
| `--service-tier` | `$RHO_AGENT_SERVICE_TIER` | OpenAI service tier |
| `--project-id` | none | Telemetry project ID |
| `--team-id` | none | Telemetry team ID |

## Examples

```bash
# New project with verification and a feature branch
rho-agent continuum prd.md \
  --working-dir ./my-project \
  --branch feature/initial-build \
  --test-cmd "pytest" \
  --max-sessions 5

# Check what the agent has been doing
cat ./my-project/.rho-agent/handoffs/002-implement-auth.md

# Continue where it left off
rho-agent continuum prd.md --resume
```

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Project completed |
| 1 | Error |
| 3 | Paused — max sessions reached (resume with `--resume`) |

## State and resumption

State is saved to `~/.config/rho-agent/continuum/{run_id}.json` after every session. When paused or interrupted, resume with `--resume` to pick up from the last saved state.

## Handoff format

Each handoff document answers:

1. **Objective** — What are we trying to achieve relative to the PRD?
2. **What I did** — Commits, test results, observations.
3. **What I tried that didn't work** — Failed approaches the next session should not repeat.
4. **Assumptions I made** — So the next session can verify them.
5. **What's weak** — Low-confidence or likely-wrong assumptions.
6. **What to verify first** — The single most valuable check before continuing.
7. **Next options** — Plausible next paths (plural), including fallbacks.

The "failed approaches" and "what's weak" fields matter most. They are what prevent the next session from blindly inheriting a bad direction.

## Architecture

```
rho_agent/continuum/
├── cli.py          # CLI entry point (continuum command)
├── models.py       # ContinuumConfig, ContinuumState, SessionUsage
├── loop.py         # Main session loop
├── handoffs.py     # Read/write handoff documents
├── checks.py       # Test/lint/typecheck runner
├── git_ops.py      # Branch creation, commits, diffs
├── state.py        # Atomic JSON persistence
└── prompts/
    └── agent.md    # Single agent prompt (Jinja2 + frontmatter)
```

The agent runs with a `developer` profile and `auto_approve=True`. The prompt is loaded from disk via the standard `rho_agent/prompts` loader — edit `agent.md` to change agent behavior without touching Python code.

## Design

See [docs/design/continuum-v2.md](../../docs/design/continuum-v2.md) for the full design rationale.
