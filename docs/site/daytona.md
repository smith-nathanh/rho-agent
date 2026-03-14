---
title: Daytona Backend
description: Run agent tools in a remote Daytona cloud sandbox.
order: 10
---

The Daytona backend routes shell and file tool execution to a remote cloud sandbox managed by [Daytona](https://daytona.io). The agent process stays on your machine — only tool calls run remotely. Any permission profile can be combined with `--backend daytona`.

## Setup

Install the SDK extra and set your API key:

```bash
uv tool install 'rho-agent[daytona]'
export DAYTONA_API_KEY=your-key
```

## CLI usage

```bash
# One-shot task in a sandbox
rho-agent main --backend daytona --profile developer "set up a Python project and run the tests"

# Interactive session
rho-agent main --backend daytona --profile developer
```

You can also set the backend via environment variable:

```bash
export RHO_AGENT_BACKEND=daytona
rho-agent main --profile developer
```

### Uploading files

Use `--upload` to stage local files into the sandbox before the agent starts. The format is `./local/path:/remote/path` and the flag is repeatable.

```bash
# Upload a project directory
rho-agent main --backend daytona --upload ./my-project:/work/my-project --profile developer

# Multiple uploads
rho-agent main --backend daytona \
  --upload ./data:/work/data \
  --upload ./config.yaml:/work/config.yaml \
  --profile developer
```

## Project configuration (`.rho-agent.toml`)

Drop a `.rho-agent.toml` file in your project root to configure uploads, setup commands, and sandbox settings. The CLI discovers it by walking up from the current directory (like `.gitignore`).

When `--backend daytona` is used and a `.rho-agent.toml` is found, the CLI automatically uploads the project, runs setup commands, and configures the sandbox — no extra CLI flags needed.

```bash
# With a .rho-agent.toml in the project root, just run:
rho-agent main --backend daytona --profile developer "run the tests"
```

### Full schema

```toml
[sandbox]
# Container image (default: "daytonaio/sandbox:latest")
image = "daytonaio/sandbox:latest"

# OR build from a Dockerfile (relative to project root)
# dockerfile = "Dockerfile"

# OR use a pre-built Daytona snapshot (fastest startup)
# snapshot = "my-snapshot-name"

# Working directory inside the sandbox (default: /home/daytona/<project-name>)
working_dir = "/home/daytona/myapp/backend"

# Setup commands run after file upload, before the agent starts.
# Commands run in working_dir. If any command fails, the agent does not start.
setup = [
    "uv sync",
    "uv run alembic upgrade head",
]

# Pin the uv version installed in the sandbox (default: latest)
# uv_version = "0.6.6"

# Environment variables set on the sandbox
[sandbox.env]
DATABASE_URL = "sqlite:///db.sqlite3"
NODE_ENV = "development"
```

### Setup commands

Setup commands are the recommended way to install dependencies — without them, the agent wastes turns figuring out what's missing. Commands run in the `working_dir` directory and execute in order. If any command exits non-zero, the agent does not start.

Make sure `working_dir` points to where your setup commands should execute (e.g. where `pyproject.toml` lives), not just the repo root.

```toml
# Example: Python project with backend in a subdirectory
[sandbox]
working_dir = "/home/daytona/trades-dash/backend"
setup = ["uv sync"]
```

```toml
# Example: monorepo with multiple setup steps
[sandbox]
working_dir = "/home/daytona/monorepo"
setup = [
    "cd backend && uv sync",
    "cd frontend && npm install",
]
```

### CLI overrides

CLI flags override `.rho-agent.toml` values:

```bash
# Override the image
rho-agent main --backend daytona --dockerfile ./Dockerfile.dev --profile developer

# Override uploads (disables auto-upload of project root)
rho-agent main --backend daytona --upload ./data:/work/data --profile developer

# Override working directory
rho-agent main --backend daytona --working-dir /home/daytona/myapp/backend --profile developer
```

### Auto-upload behavior

If no `--upload` flags are passed, the CLI automatically uploads the project root (the directory containing `.rho-agent.toml`, or the current directory) to `/home/daytona/<project-name>`. Directories like `.git`, `node_modules`, `__pycache__`, `.venv`, `venv`, and `.tox` are skipped.

## Python API

Use `Session` as an async context manager to ensure the sandbox is cleaned up when the session ends.

### Basic usage

```python
from rho_agent import Agent, AgentConfig, Session

config = AgentConfig(
    system_prompt="You are a deployment assistant.",
    backend="daytona",
    profile="developer",
)

async with Session(Agent(config)) as session:
    result = await session.run(prompt="Deploy the latest build to staging.")
    print(result.text)
# sandbox deleted automatically
```

### Custom sandbox configuration

Pass a `DaytonaBackend` for control over the container image, resources, environment variables, and auth:

```python
from daytona import DaytonaConfig, Resources
from rho_agent import Agent, AgentConfig, Session
from rho_agent.tools.handlers.daytona import DaytonaBackend

backend = DaytonaBackend(
    config=DaytonaConfig(api_key="..."),
    image="python:3.13",
    resources=Resources(cpu=2, memory=4, disk=10),
    env_vars={"MY_API_KEY": "...", "NODE_ENV": "development"},
)

async with Session(Agent(AgentConfig(profile="developer", backend=backend))) as s:
    result = await s.run(prompt="Set up the project")
    print(result.text)
```

### Using `Image` objects

For pre-built sandbox images with dependencies baked in, pass a Daytona `Image` object instead of a string:

```python
from daytona import Image
from rho_agent.tools.handlers.daytona import DaytonaBackend

image = (
    Image.debian_slim("3.13")
    .pip_install_from_pyproject("./pyproject.toml", optional_deps=["dev"])
    .workdir("/home/daytona/myapp")
)

backend = DaytonaBackend(image=image, env_vars={"MY_KEY": "..."})
```

### Uploading and downloading files

Use `get_sandbox()` to access the Daytona filesystem API directly:

```python
async with Session(Agent(AgentConfig(profile="developer", backend=backend))) as s:
    sandbox = await s.get_sandbox()

    # Upload a local file into the sandbox
    await sandbox.fs.upload_file("./my-project.tar.gz", "/work/my-project.tar.gz")

    await s.run(prompt="Extract /work/my-project.tar.gz and run the tests")

    # Download results before the sandbox is destroyed
    await sandbox.fs.download_file("/work/results.json", "./results.json")
```

## DaytonaBackend

```python
@dataclass
class DaytonaBackend:
    config: DaytonaConfig | None = None   # SDK auth config (None = env vars)
    image: str | Image = "daytonaio/sandbox:latest"  # Container image string or Image object
    snapshot: str | None = None           # Create from snapshot instead of image
    resources: Resources | None = None    # CPU, memory, disk, GPU
    auto_stop_interval: int = 0           # Auto-stop after N seconds idle (0 = never)
    env_vars: dict[str, str] = {}         # Environment variables set on the sandbox
```

A sane default `PATH` (including `~/.local/bin`) is always set on the sandbox. If you provide a custom `PATH` in `env_vars`, it overrides the default.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `DAYTONA_API_KEY` | — | API key (required, read by Daytona SDK) |
| `DAYTONA_API_URL` | Daytona default | API endpoint override (read by Daytona SDK) |

## How it works

When `--backend daytona` is set, all file and shell tools (`bash`, `read`, `write`, `edit`, `glob`, `grep`, `list`) are handled by remote equivalents that execute in the sandbox. The tool names and parameter schemas are identical — the model sees the same interface.

Database tools always run locally regardless of the backend setting.

### Sandbox lifecycle

The sandbox is lazily provisioned on the first tool call and deleted when the session closes (via `session.close()` or the async context manager). If the process crashes, the sandbox may remain — use the Daytona dashboard to clean up orphaned sandboxes.
