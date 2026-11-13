---
title: Installation
description: Environment setup, install options, and verification.
order: 3
---

## Requirements

- Python 3.11+
- `uv` (recommended)

## Install the CLI (recommended for command-line use)

```bash
uv tool install git+https://github.com/smith-nathanh/rho-agent
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

For maintainers cutting a release: the installer looks for GitHub release assets named `rho-agent-<target>.tar.gz`, each containing executables named `rho-agent` and `rho-eval`. Supported targets are macOS and Linux on `x86_64` and `arm64` (`aarch64`).

## Use as a Python library / SDK (project dependency)

Install inside your project directory so `import rho_agent` works in your app code:

```bash
uv add git+https://github.com/smith-nathanh/rho-agent
```

If you need optional features, add extras to the dependency:

```bash
uv add 'rho-agent[db] @ git+https://github.com/smith-nathanh/rho-agent'
uv add 'rho-agent[daytona] @ git+https://github.com/smith-nathanh/rho-agent'
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

### Model provider

rho-agent reads only these three for the model connection. `OPENAI_API_KEY`, `OPENAI_BASE_URL` and `OPENAI_MODEL` are ignored, so a key set for another tool is never sent anywhere by accident.

| Variable | Default | Description |
|---|---|---|
| `RHO_AGENT_API_KEY` | — | API key from your provider. Required, except for a server of your own that needs none |
| `RHO_AGENT_BASE_URL` | OpenAI | Your provider's OpenAI-compatible endpoint, e.g. `https://openrouter.ai/api/v1` |
| `RHO_AGENT_MODEL` | `gpt-6.1-terra` | Default model, as your provider names it |

### Optional

| Variable | Default | Description |
|---|---|---|
| `RHO_AGENT_PROFILE` | `readonly` | Default permission profile |
| `RHO_AGENT_BACKEND` | `local` | Agent backend — `local` or `daytona` |
| `RHO_AGENT_SERVICE_TIER` | — | API service tier (e.g. `flex`) |
| `RHO_AGENT_REASONING_EFFORT` | — | Reasoning effort level. See the [FAQ](faq/) for the limits with current OpenAI models |
| `RHO_AGENT_COMPACT_THRESHOLD` | `0.7` | Compact history at this fraction of the context window |
| `RHO_AGENT_COMPACT_AT_TOKENS` | — | Compact history at this exact input token count; wins over the fraction |
| `RHO_AGENT_AUTO_COMPACT` | `1` | Set to `0` to turn automatic compaction off |
| `RHO_AGENT_PROJECT_INSTRUCTIONS` | `1` | Set to `0` to stop reading `AGENTS.md` / `CLAUDE.md` |
| `RHO_AGENT_SKILLS` | `1` | Set to `0` to stop listing `SKILL.md` skills |
| `RHO_AGENT_PRICE_INPUT` | — | Price of input tokens, USD per million. Set with `RHO_AGENT_PRICE_OUTPUT` to price a model yourself |
| `RHO_AGENT_PRICE_CACHED_INPUT` | input price | Price of cached input tokens, USD per million |
| `RHO_AGENT_PRICE_OUTPUT` | — | Price of output tokens, USD per million |

### Cost

Every call's cost is recorded in the session's usage and trace:

- **OpenRouter** reports the cost of each call itself, and rho-agent uses that figure.
- **OpenAI** reports only tokens, so rho-agent prices them from a built-in table of OpenAI models. The table has cached-input prices, the higher prices for prompts over a model's long-context threshold, and the flex and priority service tiers. It is regenerated from LiteLLM's public price list with `python scripts/update_prices.py`; nothing is fetched at runtime.
- **Any other server** has no known price. Set the `RHO_AGENT_PRICE_*` variables to price it yourself; they also override the table. Without them cost is reported as `0`, and a session with a cost cap warns that the cap cannot trigger.

With OpenRouter, rho-agent also sends the session ID with every call (`x-session-id`), so OpenRouter's [Broadcast](https://openrouter.ai/docs/guides/features/broadcast) shows a whole session as one trace in tools such as LangSmith or Arize.

### Database connections

Database tools are configured via a YAML config file, not environment variables. See [Tools — Database tools](tools/) for the config format.

| Variable | Default | Description |
|---|---|---|
| `RHO_AGENT_DB_CONFIG` | `~/.config/rho-agent/databases.yaml` | Path to database config file |

### Daytona remote sandbox

Required for `--backend daytona`.

- CLI install: `uv tool install 'rho-agent[daytona] @ git+https://github.com/smith-nathanh/rho-agent'`
- SDK/project install: `uv add 'rho-agent[daytona] @ git+https://github.com/smith-nathanh/rho-agent'`

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
