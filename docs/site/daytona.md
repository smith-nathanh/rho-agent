---
title: Daytona Backend
description: Run agent tools in a remote Daytona cloud sandbox.
order: 9
---

The Daytona backend routes shell and file tool execution to a remote cloud sandbox managed by [Daytona](https://daytona.io). The agent process stays on your machine — only tool calls run remotely. Any permission profile can be combined with `--backend daytona`.

## Setup

Install the SDK extra and set your API key:

```bash
uv pip install 'rho-agent[daytona]'
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

Pass a `DaytonaBackend` for control over the container image, resources, and auth:

```python
from daytona import DaytonaConfig, Resources
from rho_agent import Agent, AgentConfig, Session
from rho_agent.tools.handlers.daytona import DaytonaBackend

backend = DaytonaBackend(
    config=DaytonaConfig(api_key="..."),
    image="python:3.13",
    resources=Resources(cpu=2, memory=4, disk=10),
)

async with Session(Agent(AgentConfig(profile="developer", backend=backend))) as s:
    result = await s.run(prompt="Set up the project")
    print(result.text)
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
    image: str = "ubuntu:latest"          # Container image
    resources: Resources | None = None    # CPU, memory, disk, GPU
    auto_stop_interval: int = 0           # Auto-stop after N seconds idle (0 = never)
```

## Configuration

| Variable | Default | Description |
|---|---|---|
| `DAYTONA_API_KEY` | — | API key (required) |
| `DAYTONA_API_URL` | Daytona default | API endpoint override |
| `DAYTONA_SANDBOX_IMAGE` | `ubuntu:latest` | Container image for the sandbox |
| `DAYTONA_SANDBOX_CPU` | — | CPU cores |
| `DAYTONA_SANDBOX_MEMORY` | — | Memory in MB |
| `DAYTONA_SANDBOX_DISK` | — | Disk in GB |

## How it works

When `--backend daytona` is set, all file and shell tools (`bash`, `read`, `write`, `edit`, `glob`, `grep`, `list`) are handled by remote equivalents that execute in the sandbox. The tool names and parameter schemas are identical — the model sees the same interface.

Database tools always run locally regardless of the backend setting.

### Sandbox lifecycle

The sandbox is lazily provisioned on the first tool call and deleted when the session closes (via `session.close()` or the async context manager). If the process crashes, the sandbox may remain — use the Daytona dashboard to clean up orphaned sandboxes.
