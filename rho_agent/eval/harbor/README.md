# Harbor Integration

This package exposes `rho_agent.eval.harbor.agent:RhoAgent` for running `rho-agent` inside [Harbor](https://github.com/laude-institute/harbor) jobs.

Harbor already supports loading installed agent classes by import path, so the supported integration point is:

```text
rho_agent.eval.harbor.agent:RhoAgent
```

## User Workflow

Use this when you want to run Harbor with a locally installed `rho-agent` package while task containers install `rho-agent` from git.

### Prerequisites

- Python 3.12+ for Harbor
- Docker
- API credentials exported in your shell or loaded from a `.env`

Install Harbor and install `rho-agent` from git in your local environment so Harbor can import `rho_agent.eval.harbor.agent:RhoAgent`:

```bash
uv tool install harbor
uv tool install 'git+https://github.com/smith-nathanh/rho-agent.git#egg=rho-agent[evals]'
```

Or in a project environment:

```bash
uv add harbor
uv pip install 'git+https://github.com/smith-nathanh/rho-agent.git#egg=rho-agent[evals]'
```

### Sample configs

The package ships Harbor config templates. List them with:

```bash
rho-eval harbor list-configs
```

Write one to your current directory:

```bash
rho-eval harbor write-config terminal-bench-prelim-git
rho-eval harbor write-config terminal-bench-sample
rho-eval harbor write-config terminal-bench
```

For a quick smoke test, use the git-backed hello-world config:

```bash
rho-eval harbor write-config terminal-bench-prelim-git
```

Edit `terminal-bench-prelim-git.yaml` and set:

- `version` to the branch, tag, or commit you want Harbor containers to install
- `kwargs.repo_url` to the repository Harbor should clone inside the task container

Then run Harbor:

```bash
harbor run --config ./terminal-bench-prelim-git.yaml
harbor run --config ./terminal-bench-sample.yaml
RHO_AGENT_SERVICE_TIER=flex harbor run --config ./terminal-bench.yaml
```

### How installation works inside task containers

The Harbor host process imports the agent class from your local Python environment:

```yaml
agents:
  - import_path: rho_agent.eval.harbor.agent:RhoAgent
```

Inside each task container, `RhoAgent` can install `rho-agent` either from PyPI or from git. At the moment, the supported workflow is git install via:

```yaml
agents:
  - import_path: rho_agent.eval.harbor.agent:RhoAgent
    version: main
    kwargs:
      install_source: git
      repo_url: https://github.com/smith-nathanh/rho-agent.git
```

`version` is the git ref to check out inside the task container.

### Important kwargs

Bundled configs use `kwargs` such as:

- `install_source`
- `repo_url`
- `bash_only`
- `enable_reviewer`
- `reviewer_max_iterations`
- `enable_confirm_done`
- `confirm_done_max`
- `temperature`
- `reasoning_effort`
- `cost_ceiling_usd`

## PyPI Workflow

If `rho-agent` is published to PyPI later, Harbor containers can install from PyPI instead of git:

```yaml
agents:
  - import_path: rho_agent.eval.harbor.agent:RhoAgent
    kwargs:
      install_source: pypi
```

For local iteration outside containers, editable installs are still fine:

```bash
cd ~/proj/harbor
uv pip install -e ~/proj/rho-agent
```

## Environment Variables

`RhoAgent` forwards provider configuration from the Harbor host process into the task container through `ExecInput(env=...)`. The main variables are:

- `OPENAI_API_KEY`
- `RHO_AGENT_MODEL` or `OPENAI_MODEL`
- `RHO_AGENT_BASE_URL` or `OPENAI_BASE_URL`
- `RHO_AGENT_SERVICE_TIER`

## Bundled Configs

| File | Dataset | Tasks | Use case |
|------|---------|-------|----------|
| `terminal-bench-prelim-git.yaml` | `hello-world` | 1 | Git-backed smoke test |
| `terminal-bench-prelim.yaml` | `hello-world` | 1 | PyPI-backed smoke test |
| `terminal-bench-sample.yaml` | `terminal-bench-sample` | 10 | Short validation run |
| `terminal-bench.yaml` | `terminal-bench` | 89 | Full benchmark |
