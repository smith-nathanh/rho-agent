---
title: Installation
description: Environment and install options for local development and global usage.
order: 3
---

## Requirements

- Python 3.13+
- [`uv`](https://docs.astral.sh/uv/)

## Local development install

```bash
uv sync
```

## Install with dev tools

```bash
uv sync --group dev
```

## Global CLI install

```bash
uv tool install .
```

## Verify

```bash
uv run rho-agent --help
```

If your `.venv` is broken, recreate it and run `uv sync --group dev`.
