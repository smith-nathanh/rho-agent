# Experiments

A record of every evaluation run on rho-agent: what was asked, how it was set
up, what it cost and what it found. Results that are not written down here did
not happen.

## Index

| ID | Date | Experiment | Status | Trials | Cost (USD) | Result |
|---|---|---|---|---|---|---|
| [E000](E000-tb2-sample-gpt-5.4/) | 2026-03-17 | Terminal-Bench 2.0 sample, GPT-5.4, bash only | Done | 8 (+2 hello-world) | 5.39 | 3/8 passed; 3 agent timeouts |
| [E001](E001-smoke-litellm-client/) | 2026-11-11 | Smoke test, Sol, old LiteLLM client | Done | 1 | 0.20 | Passed |
| [E002](E002-smoke-model-client/) | 2026-11-13 | Smoke test, Sol, ModelClient with reasoning carried | Done | 1 | 0.50 | Failed (one XSS case) |
| [E003](E003-openrouter-api-checks/) | 2026-11-13 | Live API checks: OpenRouter reasoning replay, cost reporting | Done | 9 API calls | 0.003 | Reasoning replay and cost both work |
| [E004](E004-pilot-tool-sets/) | 2026-11-13 | Pilot: bash + images vs full tool set, Sol | Ready, not run | 16 planned | at most 64 | — |
| [E005](E005-full-terminal-bench-4.1/) | — | Full Terminal-Bench 4.1, Sol | Planned | 66 planned | at most 264 | — |

**Spent so far: $6.10** (E000–E003). The machine-readable ledger is
[`ledger.csv`](ledger.csv).

## How to record an experiment

1. **Before running**, add a folder `E<NNN>-<short-name>/` with a `README.md`
   from [`TEMPLATE.md`](TEMPLATE.md): the question, the design, the exact
   configs (copied into `configs/`), the rho-agent commit and the budget. Add a
   row to the index above with status "Ready, not run".
2. **Pin the code.** Configs install rho-agent from git, so set `version` to a
   commit SHA, not `main`, and record it.
3. **After running**, fill in Results and Cost, update the index row and the
   total, and add a row to `ledger.csv`.

## Where costs come from

- **Harbor runs:** the `cost_usd` in each trial's `result.json` (`agent_result`),
  which comes from rho-agent's own accounting: OpenRouter's reported cost, or
  OpenAI token counts priced from `rho_agent/client/openai_prices.json`. Sum it
  over trials, or read it from the job's top-level `result.json`.
- **Ad hoc API calls:** the cost each call reported, added up by hand.
- Runs before 2026-11-13 were priced with LiteLLM's price list; the figures are
  comparable for the models used.

Job outputs live in `jobs/` in the main checkout, which is not committed. The
paths are recorded in each experiment so the raw traces can be found.

## Tools

- [`tools/compare_trials.py`](tools/compare_trials.py): compares two job
  directories task by task (reward, cost, tokens, model and tool calls,
  minutes). Standard library only.
