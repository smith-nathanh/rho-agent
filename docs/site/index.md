---
title: rho-agent
description: A configurable AI agent for software development, triage, and operations.
order: 1
---

rho-agent is a configurable AI agent for software development, debugging, and operations workflows. It provides a structured agent loop with built-in tool handlers for shell execution, file inspection, database access, and external service integration — all governed by permission profiles that control what each agent can and cannot do. Every session is traced to disk and can be resumed, inspected, or monitored live.

## Key capabilities

**Permission profiles** control shell access, file write permissions, and database mutation behavior per agent. A `readonly` agent can safely inspect production systems. A `developer` agent can edit files and run arbitrary commands. An `eval` agent has unrestricted access for sandboxed benchmark execution. The **Daytona backend** (`--backend daytona`) routes shell and file tool execution to a remote cloud sandbox.

**Native tool handlers** give agents direct access to shells, files, databases (PostgreSQL, MySQL, Oracle, Vertica, SQLite), and Excel files — without relying on external plugins or MCP servers.

**Delegation** lets agents spawn focused child agents for subtasks.

**Observability** — every session writes an append-only `trace.jsonl` with token usage, costs, and tool execution. Sessions can be listed, resumed, and inspected offline. Attach custom observers for live export to external systems.

**Monitor** — watch, pause, steer, and cancel running agents with `rho-agent monitor`.

**Prompt templates** with YAML frontmatter and Jinja2 variable substitution make it easy to define repeatable, parameterized agent tasks.

## Documentation

| Guide | Description |
|---|---|
| [Quickstart](quickstart/) | Get running in minutes |
| [Installation](installation/) | Environment setup, install options, and verification |
| [CLI Reference](cli-reference/) | Commands, flags, and usage examples |
| [Python SDK](python-sdk/) | Create and run agents programmatically |
| [Prompt Files](prompt-files/) | Template prompts with frontmatter and variables |
| [Tools](tools/) | Complete tool handler reference |
| [Profiles](profiles/) | Permission profiles and custom profile YAML |
| [Daytona](daytona/) | Remote sandbox execution via Daytona |
| [Observability](observability/) | Session traces, offline inspection, and observers |
| [Monitor](monitor/) | Watch, control, and steer running agents |
| [Architecture](architecture/) | System design, session lifecycle, and tool routing |
| [FAQ](faq/) | Common questions and troubleshooting |
