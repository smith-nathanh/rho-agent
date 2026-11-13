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
2. **Export the provider key** for the model, e.g. `OPENAI_API_KEY`. Harbor
   passes it into the container as `RHO_AGENT_API_KEY`, with the endpoint as
   `RHO_AGENT_BASE_URL` when the provider is not OpenAI. Anthropic models go
   through Anthropic's OpenAI-compatible endpoint (Chat Completions).
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

Cost is computed from token counts with the price list bundled with the
LiteLLM version installed in the container, including cached input, the
long-context price tier and flex pricing. For a model that list doesn't know,
set `RHO_AGENT_PRICE_INPUT`, `RHO_AGENT_PRICE_CACHED_INPUT` and
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

- `qemu-*` tasks were not reliable under Docker on ARM Macs in earlier runs.
- The `delegate` tool is disabled in this runner: a run measures one agent.
