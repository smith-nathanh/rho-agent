---
title: Quickstart
description: Get rho-agent running in minutes.
order: 2
---

## Install dependencies

```bash
uv sync
```

## Interactive mode

```bash
uv run rho-agent main
uv run rho-agent main --profile developer --working-dir ~/proj/myapp
```

## One-shot mode

```bash
uv run rho-agent main "what does this project do?"
```

## Useful commands

```bash
uv run rho-agent dashboard
uv run rho-agent ps
uv run rho-agent kill --all
```

Next: [CLI Reference](/docs/cli-reference/)
