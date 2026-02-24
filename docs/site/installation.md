---
title: Installation
description: Environment setup, install options, and verification.
order: 3
---

## Requirements

- Python 3.13+
- `uv` (recommended)

## Install the CLI (recommended for command-line use)

```bash
uv tool install rho-agent
```

`uv tool install` installs the `rho-agent` and `rho-eval` commands in an isolated tool environment and adds them to your `PATH`.

## Convenience installer

```bash
curl -fsSL https://rho-agent.dev/install.sh | bash
```

The hosted installer is a convenience wrapper. It will use a GitHub Releases binary when available for your platform, and otherwise fall back to `uv tool install`.

You can also pin a versioned installer URL:

```bash
curl -fsSL https://rho-agent.dev/install/v0.1.0.sh | bash
```

## Use as a Python library / SDK (project dependency)

Install inside your project directory so `import rho_agent` works in your app code:

```bash
uv add rho-agent
```

If you need optional features, add extras to the dependency:

```bash
uv add 'rho-agent[db]'
uv add 'rho-agent[daytona]'
```

## Install from source (development)

```bash
git clone https://github.com/smith-nathanh/rho-agent.git
cd rho-agent
uv sync
uv tool install .
```

This makes `rho-agent` available as a global command. If you skip `uv tool install`, prefix commands with `uv run`.

To include development tools (pytest, linters):

```bash
uv sync --group dev
```

## Verify the installation

```bash
rho-agent --help
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
| `RHO_AGENT_PROFILE` | `readonly` | Default permission profile |
| `RHO_AGENT_BACKEND` | `local` | Agent backend — `local` or `daytona` |
| `RHO_AGENT_SERVICE_TIER` | — | API service tier (e.g. `flex`) |
| `RHO_AGENT_REASONING_EFFORT` | — | Reasoning effort level (for o1/o3 models) |

### Database connections

Database tools are configured via a YAML config file, not environment variables. See [Tools — Database tools](tools/) for the config format.

| Variable | Default | Description |
|---|---|---|
| `RHO_AGENT_DB_CONFIG` | `~/.config/rho-agent/databases.yaml` | Path to database config file |

### Daytona remote sandbox

Required for `--backend daytona`.

- CLI install: `uv tool install 'rho-agent[daytona]'`
- SDK/project install: `uv add 'rho-agent[daytona]'`

See the [Daytona](daytona/) guide for configuration and environment variables.

## Troubleshooting

If your `.venv` gets into a bad state (packages installed but imports fail), reset it:

```bash
deactivate 2>/dev/null || true
rm -rf .venv
uv venv .venv --python 3.13 --seed
uv sync --group dev --python .venv/bin/python
```

Use `uv sync --python .venv/bin/python` rather than `uv sync --active` to avoid targeting the wrong virtual environment when another venv is active in your shell.
