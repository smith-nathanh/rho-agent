# E005: Full Terminal-Bench 4.1, GPT-6.1 Sol

| | |
|---|---|
| Status | Planned; waits on E006 |
| rho-agent commit | to be pinned before running |
| Dataset | `terminal-bench/terminal-bench` 4.1.0, all 66 tasks |
| Budget | worst case $264 (66 x $4 cap, one attempt); expected well below |
| Cost | — |

## Question

What does rho-agent score on Terminal-Bench 4.1 with GPT-6.1 Sol, and at what
cost per task?

## Design (draft)

- **Model:** `openai/gpt-6.1-sol` at medium reasoning effort (to be decided:
  medium vs high).
- **Tools:** the arm chosen in [E004](../E004-pilot-tool-sets/).
- **Settings:** completion gate on, $4 cost cap and a time cap set below each
  task's timeout; same system prompt for every task.
- **Repetitions:** one attempt per task (budget). The score then has a standard
  error of about 6 points.
- Start from `rho_agent/eval/harbor/configs/terminal-bench.yaml`, pin
  `version` to a commit, and copy the final config into `configs/`.

## Open decisions

- Reasoning effort. The Terminal-Bench 4.1 leaderboard runs GPT-6.1 Sol at
  max; [E006](../E006-pilot-max-effort/) measures what max costs and changes.
- Whether to exclude tasks that are unreliable under Docker on an ARM Mac
  (`qemu-*` in earlier versions).
- Whether to raise Docker Desktop's memory limit so more trials can run at once.
