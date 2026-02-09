---
title: CLI Reference
description: Commands and flags for interactive, one-shot, and operational workflows.
order: 4
---

## Main command

```bash
uv run rho-agent main [PROMPT]
```

- No prompt: starts interactive session
- With prompt: runs one-shot task and exits

## Common flags

- `--profile <readonly|developer|eval|path/to/profile.yaml>`
- `--working-dir <path>`
- `--prompt <file.md>`
- `--var key=value` (for prompt template variables)
- `--output <path>`
- `--team-id <id>`
- `--project-id <id>`

## Session and ops commands

```bash
uv run rho-agent dashboard
uv run rho-agent monitor
uv run rho-agent ps
uv run rho-agent kill --all
```

## Examples

```bash
uv run rho-agent main --profile readonly
uv run rho-agent main --profile developer --working-dir ~/proj/myapp
uv run rho-agent main --prompt examples/job-failure.md --var cluster=prod --var log_path=/mnt/logs/123
```
