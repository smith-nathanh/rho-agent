---
title: Observability
description: Session traces, offline inspection, custom observers, and OpenTelemetry export.
order: 11
---

Observability is built into the core. `State` automatically writes `trace.jsonl` to the session directory whenever you use `SessionStore`. There is nothing to enable or configure — tracing is always on.

## trace.jsonl format

Every significant event during a session is appended as a JSON line to `trace.jsonl`. Each line has a `type` field and a `ts` timestamp.

### Event types

| Type | Description |
|---|---|
| `run_start` | A new `session.run()` call begins |
| `run_end` | A `session.run()` call completes (includes status) |
| `system_prompt` | The final system prompt the model receives (`content`, including any `AGENTS.md` text and skill list) and the enabled tool names (`tools`). Written at the start of a run whenever either changed since it was last recorded |
| `message` | A message added to conversation (role: user, assistant, tool, system) |
| `llm_start` | LLM API call initiated (`purpose: compaction` for a summarising call) |
| `llm_end` | LLM API call completed (includes token counts and cost) |
| `tool_start` | Tool execution begins (includes tool name and args) |
| `tool_end` | Tool execution completes (includes `success`) |
| `approval_requested` / `approval_resolved` | A queued tool approval was filed / answered (`outcome`: `approved`, `denied` or `cancelled`) |
| `child_start` / `child_end` | A child session was spawned / finished (includes `child_session_id`, and on `child_start` the `tool_call_id` that spawned it) |
| `tool_blocked` | Tool call denied by permissions or approval |
| `compact` | Context compaction occurred (includes tokens before/after and `method`: `summary` or `prune`) |
| `usage` | Cumulative usage snapshot |

### Sample trace

```jsonl
{"type": "run_start", "run_id": 1, "prompt": "List all tables.", "ts": "2026-02-21T10:00:00Z"}
{"type": "message", "role": "user", "content": "List all tables.", "ts": "2026-02-21T10:00:00Z"}
{"type": "llm_start", "model": "gpt-6.1-terra", "context_size": 150, "ts": "2026-02-21T10:00:00Z"}
{"type": "llm_end", "model": "gpt-6.1-terra", "input_tokens": 150, "output_tokens": 45, "cache_read_tokens": 0, "cost_usd": 0.0003, "ts": "2026-02-21T10:00:01Z"}
{"type": "tool_start", "tool_name": "bash", "tool_call_id": "call_abc", "tool_args": {"command": "psql -c '\\dt'"}, "ts": "2026-02-21T10:00:01Z"}
{"type": "tool_end", "tool_name": "bash", "tool_call_id": "call_abc", "success": true, "ts": "2026-02-21T10:00:01Z"}
{"type": "llm_start", "model": "gpt-6.1-terra", "context_size": 280, "ts": "2026-02-21T10:00:02Z"}
{"type": "llm_end", "model": "gpt-6.1-terra", "input_tokens": 280, "output_tokens": 90, "cache_read_tokens": 0, "cost_usd": 0.0006, "ts": "2026-02-21T10:00:03Z"}
{"type": "message", "role": "assistant", "content": "The database has 12 tables...", "ts": "2026-02-21T10:00:03Z"}
{"type": "run_end", "run_id": 1, "status": "completed", "ts": "2026-02-21T10:00:03Z"}
```

## Session directory layout

Each session directory at `~/.config/rho-agent/sessions/<session_id>/` contains:

| File | Purpose |
|---|---|
| `config.yaml` | AgentConfig snapshot (model, access, system prompt, etc.) |
| `trace.jsonl` | Append-only event log — source of truth for the session |
| `meta.json` | Registry record for the session (see below) |
| `cancel` | Sentinel file — presence cancels the current run, then it is removed |
| `pause` | Sentinel file — presence signals pause request |
| `directives.jsonl` | Operator directives queued for the agent (JSON lines) |
| `approvals/` | Queued tool approval requests and their answers |
| `children/<id>/` | Sessions spawned by this one, each with the same layout |

### meta.json

`meta.json` is written by the process running the session and replaced atomically, so it is always safe to read. It is updated at the start and end of every run, before each model call, and when the session starts or stops waiting.

| Field | Description |
|---|---|
| `id`, `parent_session_id` | Session ID, and the spawning session's ID for a child |
| `status` | See the [status table](monitor/#ps--list-sessions) |
| `waiting_on` | Tool name while `status` is `waiting_approval` |
| `model`, `access` | Agent configuration |
| `cwd` | Working directory — the project the session belongs to |
| `pid`, `host`, `user` | Process, machine and account running the session |
| `started_at`, `last_activity` | ISO timestamps |
| `first_prompt` | First 200 characters of the first prompt |
| `run_count`, `input_tokens`, `output_tokens`, `cost_usd` | Usage so far, children included |

A killed process cannot record a final status, so readers should not trust a live `status` on its own. `rho_agent.core.session_meta.effective_status(meta)` checks the recorded `pid` and returns `dead` when the process is gone.

## Monitor

For live observation and control of running agents, see the [Monitor](monitor/) guide.

## Offline inspection

`trace.jsonl` files are plain JSON lines and can be inspected with standard tools.

### Using jq

```bash
# Count tool calls in a session
jq -s '[.[] | select(.type == "tool_start")] | length' trace.jsonl

# Show token counts and cost for each LLM call
jq 'select(.type == "llm_end") | {input_tokens, output_tokens, cost_usd}' trace.jsonl

# List distinct tool names used
jq -r 'select(.type == "tool_start") | .tool_name' trace.jsonl | sort -u

# Get total input tokens across all LLM calls
jq -s '[.[] | select(.type == "llm_end") | .input_tokens] | add' trace.jsonl
```

### Using State.from_jsonl

Replay a trace file to restore full conversation state in Python:

```python
from pathlib import Path
from rho_agent import State

trace = Path("~/.config/rho-agent/sessions/abc123/trace.jsonl").expanduser()
state = State.from_jsonl(trace.read_bytes())
print(f"Messages: {len(state.messages)}")
print(f"Usage: {state.usage}")
print(f"Status: {state.status}")
print(f"Runs completed: {state.run_count}")
```

## Observers

The `StateObserver` protocol lets you attach custom side channels to receive events in real time. Any object with an `on_event(event: dict)` method satisfies the protocol.

```python
from rho_agent import Agent, AgentConfig, Session

class MetricsCollector:
    def on_event(self, event: dict) -> None:
        if event["type"] == "llm_end":
            print(f"LLM call: {event.get('input_tokens', 0)} in, {event.get('output_tokens', 0)} out")
        elif event["type"] == "tool_end":
            print(f"Tool {event['tool_name']} {'ok' if event.get('success') else 'failed'}")

agent = Agent(AgentConfig())
session = Session(agent)
session.state.add_observer(MetricsCollector())

result = await session.run(prompt="Analyze the codebase.")
```

Observers receive every event that is written to `trace.jsonl`, making them suitable for live dashboards, metrics collection, alerting, or custom logging pipelines.

## ATIF export

Session traces can be exported to [ATIF](https://github.com/laude-institute/harbor) (Agent Trajectory Interchange Format), a structured JSON schema for agent trajectories designed for SFT/RL training data.

```bash
# Export a session to stdout
rho-agent export <session-id> --dir ~/.config/rho-agent/sessions

# Write to a file
rho-agent export <session-id> -d ~/.config/rho-agent/sessions -o trajectory.json
```

```python
from rho_agent.export.atif import trace_to_atif

trajectory = trace_to_atif(
    "path/to/trace.jsonl",
    session_id="abc123",
    model_name="gpt-6.1-terra",
)
```

The export maps trace events to ATIF steps, groups tool calls with their results, and populates per-step and session-level metrics. See [`rho_agent/export/`](../../rho_agent/export/README.md) for details and the [CLI reference](cli-reference.md#rho-agent-export) for all flags.

## OpenTelemetry export

Session traces can be exported as OpenTelemetry spans and sent to any backend that accepts OTLP over HTTP. A model gateway's request logs show only model calls; this export covers the whole agent: every run, model call and tool call, with child sessions nested under the tool call that started them.

Spans follow the [OpenTelemetry GenAI semantic conventions](https://github.com/open-telemetry/semantic-conventions-genai). Those conventions are still in Development status, so attribute names may change. The names used here were checked against that repository in November 2026.

### What is exported

| Span | Kind | From | Key attributes |
|---|---|---|---|
| `invoke_agent` (top level), `invoke_agent <tool>` (child) | INTERNAL | `run_start` → `run_end` | `gen_ai.operation.name`, `gen_ai.conversation.id`, `gen_ai.request.model`, `rho_agent.run.status`, run totals as `rho_agent.usage.*` |
| `chat <model>` | CLIENT | `llm_start` → `llm_end` or `llm_error` | `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.usage.cache_read.input_tokens`, `gen_ai.usage.reasoning.output_tokens`, `rho_agent.cost_usd`, `gen_ai.usage.cost` |
| `execute_tool <tool>` | INTERNAL | `tool_start` → `tool_end`, or `tool_blocked` | `gen_ai.tool.name`, `gen_ai.tool.call.id`, `gen_ai.tool.type`; for `delegate` and agent tools also `gen_ai.transfer.mode = return_to_caller` and `rho_agent.child_session_id` |

- **One trace per run.** Every span of a `session.run()` call, child sessions included, shares one trace. `gen_ai.conversation.id` (the session ID) and `session.id` (the top-level session ID) group a session's runs. Phoenix and Langfuse use `session.id` for their session views.
- **Child sessions** are `invoke_agent` spans whose parent is the `execute_tool delegate` (or agent tool) span that spawned them, as in the conventions' agent-as-a-tool example.
- **Errors** set the span status to ERROR with an `error.type`:
  - the `llm_error` kind (`transient`, `context_overflow`, ...) for a failed model call
  - `tool_error` for a tool that reported failure
  - `blocked` for a call denied by permissions or approval
  - `interrupted` for a tool call that never finished
  - `unfinished` for a run with no `run_end` (the process was killed)

  Cancelled, `max_turns` and `budget_exceeded` runs are deliberate stops. They keep an UNSET status, and `rho_agent.run.status` says why the run stopped.
- **Compaction** calls are `chat` spans with `rho_agent.purpose = compaction`. Model calls after a compaction carry `gen_ai.conversation.compacted = true`. Approvals, compactions and completion-gate rounds are span events on the run span.
- **Usage totals** for a run (children and compaction included) are on the run span as `rho_agent.usage.*`, not `gen_ai.usage.*`. Backends that add up `gen_ai.usage` over a trace therefore do not count tokens twice. Cost is the custom attribute `rho_agent.cost_usd`. The same value is also written as `gen_ai.usage.cost`, which is not in the conventions but is the attribute Langfuse reads. When rho-agent has no price for the model (it records `cost_usd: 0`), both are left out, so a backend with its own price table, such as Langfuse, computes the cost instead of showing $0.
- **IDs are deterministic.** Trace and span IDs are derived from the session ID, run number and tool call ID. Exporting a session twice, or exporting it after the live exporter has sent it, gives the same IDs. Whether a backend merges a second copy depends on the backend: Phoenix ignores spans it already has, but Langfuse v4 stores both copies. Send each session once, either live or with `rho-agent export`.

### Message content

Prompts, model output, and tool arguments and results are **left out by default**. To include them, pass `--include-content`, or set `RHO_AGENT_OTLP_CONTENT=1` for live export. This adds the conventions' opt-in attributes:

- `gen_ai.input.messages` and `gen_ai.output.messages` on run and model-call spans
- `gen_ai.tool.call.arguments` and `gen_ai.tool.call.result` on tool spans

Structured values are sent as JSON strings. Each model-call span carries the whole history the model was given, so including content makes exports much larger. Text parts are clipped at 16,000 characters.

### Export a finished session

```bash
# OTLP/JSON to stdout or a file
rho-agent export <session-id> --format otlp
rho-agent export <session-id> --format otlp --include-content -o spans.json

# Send to an OTLP/HTTP endpoint (/v1/traces is appended to the base URL)
export RHO_AGENT_OTLP_HEADERS="x-api-key=..."
rho-agent export <session-id> --format otlp --endpoint https://collector.example.com:4318
```

`--protocol` is `http/protobuf` (the default) or `http/json`. Protobuf needs the `otel` extra. JSON needs no extra packages, but not every backend accepts it (Phoenix does not).

```bash
uv pip install -e ".[otel]"
```

```python
from rho_agent.export.otlp import send_spans, trace_to_otlp, trace_to_spans

request = trace_to_otlp("path/to/trace.jsonl", session_id="abc123")  # OTLP/JSON dict
spans = trace_to_spans("path/to/trace.jsonl", session_id="abc123", include_content=True)
send_spans(spans, "http://localhost:4318", protocol="http/json")
```

### Live export

Set `RHO_AGENT_OTLP_ENDPOINT` and every session, child sessions included, sends its spans while it runs. The spans, IDs included, are the same as the offline export.

A background thread sends queued spans every two seconds and at the end of each run, so a slow or unreachable collector never blocks the agent. If an export fails, the error is logged once and those spans are dropped. When the process exits, pending spans are flushed for up to five seconds.

A run's `invoke_agent` span is sent when the run ends. If the process is killed mid-run, the model and tool calls that finished were already sent, but the run span never is, so they appear without their parent. `rho-agent export --format otlp --endpoint ...` sends the run (marked `unfinished`) afterwards, with the caveat about backends that store duplicates.

| Variable | Default | Description |
|---|---|---|
| `RHO_AGENT_OTLP_ENDPOINT` | — | Base OTLP/HTTP URL; setting it turns live export on |
| `RHO_AGENT_OTLP_HEADERS` | — | Request headers as `key=value,key2=value2` (values URL-encoded). `rho-agent export --endpoint` uses them too |
| `RHO_AGENT_OTLP_PROTOCOL` | `http/protobuf` | `http/protobuf` (needs the `otel` extra) or `http/json` |
| `RHO_AGENT_OTLP_CONTENT` | `0` | `1` to include message content |

Only `RHO_AGENT_*` variables are read. The standard `OTEL_EXPORTER_OTLP_*` variables are ignored, as `OPENAI_API_KEY` is. From Python, attach the observer yourself with `rho_agent.export.otlp_observer.maybe_attach_otlp_observer(session)`, or build an `OtlpObserver`.

### Backends

| Backend | Status | Endpoint and headers |
|---|---|---|
| [Arize Phoenix](https://github.com/Arize-ai/phoenix) 21.1 (Docker) | **Tested** (offline and live, `http/protobuf`) | `http://localhost:6006`. Phoenix maps the GenAI attributes to its agent, LLM and tool span kinds, token counts, input/output messages and tool parameters. It rejects `http/json` with a 415 |
| [OpenTelemetry Collector](https://github.com/open-telemetry/opentelemetry-collector) 0.165 | **Tested** (`http/protobuf` and `http/json`) | `http://localhost:4318`; forward to any backend the Collector supports |
| [LangSmith](https://docs.langchain.com/langsmith/trace-with-opentelemetry) | Not tested | `https://api.smith.langchain.com/otel` with headers `x-api-key=<key>,Langsmith-Project=<project>`. According to its docs, LangSmith reads the current `gen_ai.*` names (messages, cache and reasoning tokens) without OpenLLMetry names, and it maps no cost attribute |
| [Langfuse](https://langfuse.com/integrations/native/opentelemetry) 4.80 (self-hosted, Docker) | **Tested** (offline and live, `http/protobuf`, including a real model session with two parallel child agents) | `https://cloud.langfuse.com/api/public/otel` (or your host's `/api/public/otel`) with headers `Authorization=Basic%20<base64 public:secret>,x-langfuse-ingestion-version=4`. Runs become AGENT observations, model calls GENERATION and tool calls TOOL. Child agents nest under their tool call, and runs group into sessions by `session.id`. Inputs and outputs, tokens (cached and reasoning split out), cost and tool errors all map. Langfuse Cloud itself was not tested |
| Arize AX, ClickHouse ClickStack, Datadog, other OTLP intakes | Not tested | Use the vendor's OTLP/HTTP trace endpoint, or put an OpenTelemetry Collector in front of it |

A backend that ignores GenAI attributes still shows the span tree, timings and errors.
