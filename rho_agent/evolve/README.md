# rho-agent evolve

An evolutionary loop for iteratively building and improving task-agents. Inspired by the [HyperAgents](https://arxiv.org/abs/2603.19461) paper (Zhang et al., 2026), adapted to rho-agent's config-first architecture.

## How it works

A **meta-agent** iteratively mutates a **workspace** (system prompt, tools, supporting code) that defines a task-agent. A **domain harness** evaluates each variant against a set of scenarios and produces a score. The loop maintains an archive of all generations and selects parents for the next iteration based on score.

```
                    ┌─────────────┐
                    │   Archive   │
                    │  (JSONL)    │
                    └──────┬──────┘
                           │ select parent
                           ▼
┌──────────┐    ┌─────────────────────┐    ┌──────────────┐
│  Domain  │◄───│     Meta-Agent      │───►│  Workspace   │
│  Harness │    │ (mutates workspace) │    │  prompt.md   │
│          │    └─────────────────────┘    │  tools/      │
│ scenarios│                               │  lib/        │
│ scoring  │◄──── build agent from ────────┘              │
│ feedback │      workspace                               │
└──────────┘                                              │
      │                                                   │
      └──── eval results ──► eval_results.json ──────────►│
```

Each generation:
1. Select a parent from the archive (best score by default)
2. Copy parent workspace
3. Run meta-agent to make one targeted improvement
4. Staged eval on a small subset (quick filter)
5. Full eval on all scenarios
6. Append to archive

The meta-agent has unrestricted access to the workspace via bash/read/write/edit/glob/grep. It can modify `prompt.md`, create `ToolHandler` subclasses in `tools/`, or add helper modules in `lib/`.

## Mutation hierarchy

The meta-agent prompt encourages improvements in this priority order:
1. **Prompt tweaks** — lowest risk, fastest iteration
2. **Tool modifications** — fix or improve existing tools
3. **New tools** — add ToolHandler subclasses
4. **Supporting code** — helper modules in `lib/`

## Usage

```bash
rho-agent evolve <harness> [options]
```

Example with the built-in paper review harness:
```bash
rho-agent evolve rho_agent.evolve.harnesses.paper_review.PaperReviewHarness \
  --model gpt-5-mini \
  --max-generations 10 \
  --harness-arg train_n=50 \
  --harness-arg val_n=20
```

Options:
- `--run-dir` — output directory (default: `./evolve-runs`)
- `--model` — meta-agent model
- `--task-model` — task-agent model (default: inherit from `--model`)
- `--max-generations` — number of iterations
- `--staged-sample` — scenarios for quick filtering
- `--seed` — path to a seed workspace directory
- `--harness-arg` — `key=value` pairs passed to harness constructor

## Writing a domain harness

Subclass `DomainHarness` and implement three methods:

```python
from rho_agent.evolve import DomainHarness

class MyHarness(DomainHarness):
    def scenarios(self) -> list[dict]:
        """Return evaluation scenarios."""
        ...

    async def run_agent(self, agent, scenario) -> dict:
        """Run agent on one scenario. You own the Session lifecycle."""
        ...

    def score(self, results: list[dict]) -> float:
        """Aggregate score from all results."""
        ...
```

Optionally override `feedback()` for domain-specific analysis and `staged_sample()` for custom quick-filter subsets.

## Differences from HyperAgents

- **Config-first, not code-first.** HyperAgents puts the task agent and meta agent in one editable Python file. We separate concerns: the workspace holds the task-agent definition (prompt + tools + code), the meta-agent is a standard rho-agent Session, and the harness is a pluggable ABC.
- **Structured feedback.** HyperAgents dumps raw results into the container for the meta-agent to discover via bash. We inject `harness.feedback()` directly into the meta-agent prompt and write `eval_results.json` to the workspace for deeper inspection.
- **Structured mutation space.** The workspace layout (`prompt.md`, `tools/`, `lib/`) gives the meta-agent a clear hierarchy of what to change, rather than an arbitrary codebase.
- **No self-referential modification.** HyperAgents allows the meta-agent to modify itself (metacognitive self-modification). We keep the meta-agent fixed — only the task-agent workspace is mutable. This is a simplification; self-referential modification is a potential extension.

## Initial results

See [RESULTS.md](RESULTS.md) for first experimental results on the paper review domain.
