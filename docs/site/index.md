---
title: rho-agent
description: A configurable agent runtime for software development, triage, and operations.
order: 1
---

rho-agent is a configurable runtime for deploying AI agents across software development, debugging, and operations workflows. It provides a structured agent loop with built-in tool handlers for shell execution, file inspection, database access, and external service integration — all governed by permission profiles that control what each agent can and cannot do.

## Key capabilities

**Capability profiles** control shell access, file write permissions, and database mutation behavior per agent. A `readonly` agent can safely inspect production systems. A `developer` agent can edit files and run arbitrary commands. An `eval` agent has unrestricted access for sandboxed benchmark execution. A `daytona` agent executes all tools in a remote cloud sandbox.

**Native tool handlers** give agents direct access to shells, files, databases (PostgreSQL, MySQL, Oracle, Vertica, SQLite), and Excel files — without relying on external plugins or MCP servers.

**Session management** lets agents delegate focused subtasks to child agents. The monitor and cancel commands provide operational control over running sessions.

**Built-in observability** tracks sessions, turns, token usage, dollar costs, and tool execution metrics to trace.jsonl files in session directories — fully self-hosted, no data leaves your infrastructure. Supports observers for custom side channels. Includes an interactive monitor and operational control plane for managing running agents.

**Prompt templates** with YAML frontmatter and Jinja2 variable substitution make it easy to define repeatable, parameterized agent tasks.

## Documentation

| Guide | Description |
|---|---|
| [Quickstart](quickstart/) | Get running in minutes |
| [Installation](installation/) | Environment setup, install options, and verification |
| [CLI Reference](cli-reference/) | Commands, flags, and usage examples |
| [API Reference](api-reference/) | Programmatic Python interface for embedding agents |
| [Prompt Files](prompt-files/) | Template prompts with frontmatter and variables |
| [Tools](tools/) | Complete tool handler reference |
| [Profiles](profiles/) | Capability profiles and custom profile YAML |
| [Observability](observability/) | Telemetry, dashboard, and monitor |
| [Architecture](architecture/) | System design, session lifecycle, and tool routing |
| [FAQ](faq/) | Common questions and troubleshooting |
