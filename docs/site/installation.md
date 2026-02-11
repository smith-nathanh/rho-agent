---
title: Installation
description: Environment setup, install options, and verification.
order: 3
---

## Requirements

- Python 3.13+
- [uv](https://docs.astral.sh/uv/) package manager

## Install from source

```bash
git clone https://github.com/smith-nathanh/rho-agent.git
cd rho-agent
uv sync
```

To include development tools (pytest, linters):

```bash
uv sync --group dev
```

## Global CLI install

Install rho-agent as a global command available outside the project directory:

```bash
uv tool install .
```

After this, `rho-agent` is available directly without `uv run`.

## Verify the installation

```bash
uv run rho-agent --help
```

## Environment variables

### Required

| Variable | Description |
|---|---|
| `OPENAI_API_KEY` | API key for the model provider |

### Optional

| Variable | Default | Description |
|---|---|---|
| `OPENAI_BASE_URL` | OpenAI default | API endpoint override (for compatible providers) |
| `OPENAI_MODEL` | `gpt-5-mini` | Default model |
| `RHO_AGENT_PROFILE` | `readonly` | Default capability profile |
| `RHO_AGENT_TEAM_ID` | — | Team ID for observability |
| `RHO_AGENT_PROJECT_ID` | — | Project ID for observability |
| `RHO_AGENT_OBSERVABILITY_CONFIG` | — | Path to `observability.yaml` |
| `RHO_AGENT_REASONING_EFFORT` | — | Reasoning effort level (for o1/o3 models) |
| `RHO_AGENT_SIGNAL_DIR` | `~/.config/rho-agent/signals/` | Signal directory for agent coordination |

### Database connections

Database tools are enabled automatically when their environment variables are set.

| Database | Variables |
|---|---|
| SQLite | `SQLITE_DB` |
| PostgreSQL | `POSTGRES_HOST`, `POSTGRES_DATABASE`, `POSTGRES_USER`, `POSTGRES_PASSWORD` |
| MySQL | `MYSQL_HOST`, `MYSQL_DATABASE`, `MYSQL_USER`, `MYSQL_PASSWORD` |
| Oracle | `ORACLE_DSN`, `ORACLE_USER`, `ORACLE_PASSWORD` |
| Vertica | `VERTICA_HOST`, `VERTICA_DATABASE`, `VERTICA_USER`, `VERTICA_PASSWORD` |

### Azure DevOps integration

| Variable | Description |
|---|---|
| `AZURE_DEVOPS_ORG` | Organization name |
| `AZURE_DEVOPS_PAT` | Personal access token |
| `AZURE_DEVOPS_PROJECT` | Default project (optional) |
| `AZURE_DEVOPS_READONLY` | Set to `true` to disable mutations |

## Troubleshooting

If your `.venv` gets into a bad state (packages installed but imports fail), reset it:

```bash
deactivate 2>/dev/null || true
rm -rf .venv
uv venv .venv --python 3.13 --seed
uv sync --group dev --python .venv/bin/python
```

Use `uv sync --python .venv/bin/python` rather than `uv sync --active` to avoid targeting the wrong virtual environment when another venv is active in your shell.
