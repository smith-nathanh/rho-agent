# rho-agent on Harbor (Terminal-Bench)

`RhoAgent` is a Harbor installed agent: Harbor installs rho-agent inside each
task container and runs `rho_agent.eval.harbor.runner` there. The runner uses
the same model client as the CLI and Python API: OpenAI's Responses API, with
the model's reasoning carried from one call to the next. Written against
Harbor 0.25; Harbor needs Python 3.12 or newer.

```bash
uv pip install -e ".[harbor]"
```

## Before a run

1. **Push the code you want evaluated.** The container installs rho-agent from
   git (`install_source: git`, `version: <branch, tag or commit>`) or from PyPI.
   A local, unpushed branch cannot be installed.
2. **Export the provider key** for the model: `OPENAI_API_KEY` for
   `openai/...` models, `OPENROUTER_API_KEY` for `openrouter/...` models.
   Harbor passes it into the container as `RHO_AGENT_API_KEY`, with the
   endpoint as `RHO_AGENT_BASE_URL`.
3. **Validate without running anything:**

   ```bash
   harbor run --config terminal-bench-subset.yaml --dry-run
   harbor agent schema rho_agent.eval.harbor.agent:RhoAgent
   ```

## Running

With a bundled config:

```bash
rho-eval harbor list-configs
rho-eval harbor write-config terminal-bench-subset -o terminal-bench-subset.yaml
harbor run --config terminal-bench-subset.yaml
```

| Config | Dataset | Tasks |
|---|---|---|
| `terminal-bench-subset.yaml` | `terminal-bench/terminal-bench@4.1.0` | 20 |
| `terminal-bench.yaml` | `terminal-bench/terminal-bench@4.1.0` | 66 |

Or directly:

```bash
harbor run -d terminal-bench/terminal-bench@4.1.0 \
  -a rho_agent.eval.harbor.agent:RhoAgent -m openai/gpt-6.1-sol \
  --ak install_source=git --ak version=main \
  --ak bash_only=true --ak cost_ceiling_usd=4 --ak max_wall_seconds=27000
```

## Running on Daytona v2

Harbor's built-in `daytona` environment uses the Daytona v0 platform, which
caps each sandbox at 4 vCPU, 8 GiB and 10 GiB: Terminal-Bench 4.1,
Terminal-Bench-Science and SWE-Marathon all have tasks above that. Daytona v2
allows up to 16 cores and 32 GiB per sandbox. rho-agent's `daytona` extra
installs Daytona SDK 2.x, which also disables Harbor's built-in Daytona
environment, so jobs on Daytona use rho-agent's environment by import path:

```yaml
environment:
  import_path: rho_agent.eval.harbor.daytona_v2:DaytonaV2Environment
  delete: true
```

or `harbor run ... -e rho_agent.eval.harbor.daytona_v2:DaytonaV2Environment`.
`DAYTONA_API_KEY` must be a Daytona v2 key that can write sandboxes and images.

| | |
|---|---|
| Supported | Tasks built from a Dockerfile or a `docker_image`; the task's CPU, memory and disk; separate verifier environments; public, no-network and allow-list (hostnames, IPv4) network policies, switched between agent and verifier phases |
| Not supported | docker-compose (multi-container) tasks, GPUs, Windows, wildcard hostnames in allow lists |

Daytona v2 sizes CPU in cores, and one core is 2 vCPUs. A task's `cpus` are
vCPUs, so a task asking for 8 gets a 4-core sandbox, which shows 8 CPUs to
`nproc`. The sandbox is held to the task's CPU, memory and disk (they are set
as both the request and the limit). The image is pulled or built once per
content and reused, so `force_build` has no effect.

Each sandbox has a 24-hour wall-clock limit, after which Daytona v2 deletes it.
It is never stopped for being idle.

Daytona v2 adds its own minimal git as `/usr/local/bin/git` (no
`git submodule`, among others), ahead of the image's git on `PATH`. When the
task image has its own git, the environment renames Daytona's to
`/usr/local/bin/daytona-git` and makes `/usr/local/bin/git` a link to the
image's git, so tasks and Daytona's git service both use it. For the same
reason, `install_source: git` installs rho-agent from GitHub's source archive
of the ref rather than with git.

## Options

Pass as `--ak key=value` or under `kwargs` in a job config.

| Option | Default | Meaning |
|---|---|---|
| `install_source` | `pypi` | `pypi` or `git` |
| `repo_url` | this repo | Repository for `install_source: git` |
| `version` | latest | PyPI version, or git branch, tag or commit |
| `bash_only` | `false` | Only the bash tool |
| `view_image` | on, off with `bash_only` | The image-viewing tool. Set `true` with `bash_only` for bash plus images |
| `reasoning_effort` | provider default | Also settable with Harbor's `--effort` |
| `enable_confirm_done` | `true` | Completion gate: the agent must confirm it verified its work |
| `confirm_done_max` | `3` | Maximum gate rounds |
| `cost_ceiling_usd` | `0` (off) | Stop the agent at this cost per task |
| `max_wall_seconds` | `0` (off) | Stop the agent cleanly after this long; set below the task timeout |
| `max_call_seconds` | `1200` | Abandon and retry one model call after this long |
| `auto_compact` | `true` | Summarize older history automatically when the context grows large |
| `compact_threshold` | `0.7` | Compact at this fraction of the context window |
| `compact_at_tokens` | unset | Compact at this exact input token count; wins over the fraction |
| `enable_reviewer` | `false` | Separate review pass after the agent finishes |
| `reviewer_max_iterations` | `1` | Review-and-revise loops |
| `temperature` | provider default | Sampling temperature |

The cost and time caps are enforced inside the agent loop, between every step.
A capped run exits normally, so the verifier still grades whatever exists.

## Cost

The worst case for a job is `cost_ceiling_usd` x tasks x `n_attempts`.
Terminal-Bench 4.x allows each task up to 8 hours, so always set a ceiling.

Through OpenRouter, cost is what OpenRouter reports for each call. For OpenAI
models it is token counts priced from rho-agent's built-in OpenAI table, which
includes cached input, the long-context price tier and flex pricing. For any
other model, set `RHO_AGENT_PRICE_INPUT`, `RHO_AGENT_PRICE_CACHED_INPUT` and
`RHO_AGENT_PRICE_OUTPUT` (USD per million tokens) under the agent's `env` in the
job config. With a ceiling set and no price known, the runner exits before
making any model call rather than run uncapped.

## Output

Written to the trial's agent log directory as the run proceeds, so a killed
trial still has them:

| File | Contents |
|---|---|
| `trace.jsonl` | Append-only event log (messages, model calls, tool calls) |
| `trajectory.json` | ATIF trajectory, rewritten after every tool call |
| `tokens.json` | Running token and cost totals |
| `stdout.txt` | Console output |

## Notes

- Every task gets the same system prompt: the runner does not read an
  `AGENTS.md`, `CLAUDE.md` or skills folder that a task ships with. The exact
  prompt and tool list are recorded as a `system_prompt` event in `trace.jsonl`.
- Skills: when a task sets `environment.skills_dir`, the adapter copies it to
  `~/.agents/skills` and the runner lists those skills in the prompt.
- MCP servers: a task's `environment.mcp_servers` (sse, streamable-http or
  stdio, with headers) reach the runner as JSON in `RHO_AGENT_MCP_SERVERS`,
  and the `mcp` extra is installed. Their tools are offered as
  `mcp__<server>__<tool>`; a server that cannot be reached is skipped with a
  warning in `stdout.txt`.

- `qemu-*` tasks were not reliable under Docker on ARM Macs in earlier runs.
- The `delegate` tool is disabled in this runner: a run measures one agent.
