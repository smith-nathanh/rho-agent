# README demos

The two recordings in the top-level README are real sessions against a real model, scripted with [vhs](https://github.com/charmbracelet/vhs) so they can be re-recorded when the CLI changes.

```bash
docs/demo/record.sh cli       # docs/demo/cli.gif
docs/demo/record.sh monitor   # docs/demo/monitor.gif
```

You need `vhs`, `ffmpeg`, the project virtualenv (`uv sync --group dev`) and `RHO_AGENT_API_KEY` in the environment or in `.env`. Each run makes a handful of model calls.

## What the script does

- Copies the sample projects in `projects/` into a throwaway home directory, `/Users/Shared/rho-agent-demo` on macOS (set `DEMO_HOME` to use another). It is not under `/tmp` because temp directories are writable inside the sandbox, and the monitor demo needs agents that have to ask. The recorded shell uses that as `HOME`, so none of your own sessions, config or paths appear on screen.
- `cli`: types into an interactive `rho-agent` session in `projects/shop`. Two checkout tests fail there, and the cause is in a different module from the one the tests exercise (`money.py` rounds half a cent to even, not up).
- `monitor`: starts two unattended agents with `--access read-only`, each with something to fix. Fixing needs write access, so each asks for it. It waits until both are blocked on an access request, then records `rho-agent monitor` with their session IDs filled into [`monitor.tape`](monitor.tape).
- Converts the video to a GIF, cut a few seconds after the screen last changed.
- Checks that nothing in this repository changed while the agents ran, and fails if something did. The agents only ever work on the copies.

`projects/orders` is not recorded; the Python example in the top-level README reads it.

## Retakes

The model's output differs on every run, so a take can come out too long, or an agent can finish before the recording gets to it. Run the script again and keep the take you like. Timing lives in the `Sleep` lines of the two `.tape` files.
