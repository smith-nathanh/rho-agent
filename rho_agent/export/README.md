# Export

Utilities for converting rho-agent session traces into external formats.

- `atif.py`: ATIF trajectories for training data (below).
- `otlp.py`: OpenTelemetry spans (OTLP) for observability tools, following the GenAI semantic conventions. `otlp_observer.py` sends the same spans live when `RHO_AGENT_OTLP_ENDPOINT` is set. See [Observability — OpenTelemetry export](../../docs/site/observability.md#opentelemetry-export).

## ATIF (Agent Trajectory Interchange Format)

ATIF is a structured JSON schema for agent trajectories, designed for SFT/RL training data. It is defined and maintained by [Harbor](https://github.com/laude-institute/harbor).

`rho_agent/export/atif.py` converts a session's `trace.jsonl` into an ATIF-compatible JSON document.

### CLI

```bash
# Export a session to stdout
rho-agent export <session-id> --dir ~/.config/rho-agent/sessions

# Write to a file
rho-agent export <session-id> --dir ~/.config/rho-agent/sessions -o trajectory.json
```

### Python

```python
from rho_agent.export.atif import trace_to_atif

trajectory = trace_to_atif(
    "path/to/trace.jsonl",
    session_id="abc123",
    model_name="gpt-6.1-terra",
)
```

### What gets exported

- User, system, and assistant messages become ATIF **steps** (1-indexed)
- Assistant tool calls and their results are grouped into a single agent step with `tool_calls` and `observation.results` linked by call ID
- Per-step token usage and cost from `llm_end` events populate step `metrics`
- The session-level `usage` event becomes `final_metrics`
- Informational events (`run_start`, `run_end`, `tool_start`, `tool_end`, `compact`) are skipped

### ATIF spec

See the [Harbor repository](https://github.com/laude-institute/harbor) for the full ATIF schema, Pydantic models, and RFC.
