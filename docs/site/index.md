---
title: rho-agent
description: A configurable agent runtime for software development, triage, and operations.
order: 1
---

rho-agent is a configurable runtime for deploying AI agents across software development, debugging, and operations workflows. It provides a structured agent loop with built-in tool handlers for shell execution, file inspection, database access, and external service integration — all governed by capability profiles that control what each agent can and cannot do.

## Key capabilities

**Capability profiles** control shell access, file write permissions, and database mutation behavior per agent. A `readonly` agent can safely inspect production systems. A `developer` agent can edit files and run arbitrary commands. An `eval` agent has unrestricted access for sandboxed benchmark execution. A `daytona` agent executes all tools in a remote cloud sandbox.

**Native tool handlers** give agents direct access to shells, files, databases (PostgreSQL, MySQL, Oracle, Vertica, SQLite), Excel files, and Azure DevOps — without relying on external plugins or MCP servers.

**Multi-agent coordination** lets agents delegate focused subtasks to child agents and lets operators connect running agents for cross-context collaboration through the monitor.

**Built-in observability** tracks sessions, turns, token usage, and tool execution metrics to a SQLite backend or OTLP endpoint, with a Streamlit dashboard and interactive monitor for live inspection.

**Prompt templates** with YAML frontmatter and Jinja2 variable substitution make it easy to define repeatable, parameterized agent tasks.

## Documentation

| Guide | Description |
|---|---|
| [Quickstart](quickstart/) | Get running in minutes |
| [Installation](installation/) | Environment setup, install options, and verification |
| [CLI Reference](cli-reference/) | Commands, flags, and usage examples |
| [Runtime API](runtime-api/) | Programmatic Python interface for embedding agents |
| [Prompt Files](prompt-files/) | Template prompts with frontmatter and variables |
| [Tools](tools/) | Complete tool handler reference |
| [Profiles](profiles/) | Capability profiles and custom profile YAML |
| [Observability](observability/) | Telemetry, dashboard, and monitor |
| [Architecture](architecture/) | System design, agent loop, and signal protocol |
| [FAQ](faq/) | Common questions and troubleshooting |
