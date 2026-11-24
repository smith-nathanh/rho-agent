# README demos

The two recordings in the top-level README are real sessions against a real model, scripted with [vhs](https://github.com/charmbracelet/vhs) so they can be re-recorded when the CLI changes.

```bash
RHO_AGENT_MODEL=thinkingmachines/inkling-2-small docs/demo/record.sh cli
RHO_AGENT_MODEL=thinkingmachines/inkling-2-small docs/demo/record.sh monitor
```

You need `vhs`, `ffmpeg`, the project virtualenv (`uv sync --group dev`) and `RHO_AGENT_API_KEY` in the environment or in `.env`. The README takes use the Glacier theme and Inkling 2 Small. Set `RHO_AGENT_BASE_URL=https://openrouter.ai/api/v1` with the matching key in `.env` or the environment. Each run makes a handful of model calls.

## What the script does

- Copies the sample projects in `projects/` into a throwaway home directory, `/Users/Shared/rho-agent-demo` on macOS (set `DEMO_HOME` to use another). It is not under `/tmp` because temp directories are writable inside the sandbox, and the monitor demo needs agents that have to ask. The recorded shell uses that as `HOME`, so none of your own sessions, config or paths appear on screen.
- `cli`: types into an interactive `rho-agent` session in `projects/telemetry` (with a generated `telemetry.db` SQLite database). `watchdog.py` reports all nodes nominal even though `alerts.log` shows repeated resets on `node-04` caused by thermal spikes above `policy.toml`'s `max_temp_c`. The agent correlates the log with SQLite, adds the thermal guard to `watchdog.py`, re-runs it, and finishes with `/status`.
- `monitor`: starts two unattended agents with `--access read-only` in `projects/billing` and `projects/api-server` (which spawns two parallel `delegate` subagents). Fixing needs write access, so each queues an approval request. Once both are waiting, it records `rho-agent monitor` showing `ps`, `inspect` (session history & subagent tree), and `live` (Live Mission Control with fleet navigation, single-key approval, and a mid-run directive).
- Converts the recorded video to a GIF.
- Checks that nothing in this repository changed while the agents ran, and fails if something did. The agents only ever work on the copies.

`projects/orders` is not recorded; the Python example in the top-level README reads it.

## Retakes

The model's output differs on every run, so a take can come out too long, or an agent can finish before the recording gets to it. Run the script again and keep the take you like. Timing lives in the `Sleep` lines of the two `.tape` files.
