---
title: Architecture
description: Runtime shape and core system components.
order: 10
---

At a high level, rho-agent is composed of:

1. CLI/runtime entrypoints for interactive and programmatic execution.
2. Agent loop with event streaming and tool routing.
3. Tool handlers for shell, files, and databases.
4. Optional observability stack for sessions, turns, and tool metrics.

Key code areas:

- `rho_agent/runtime/` for runtime lifecycle and APIs
- `rho_agent/tools/` for tool handlers and registry
- `rho_agent/observability/` for telemetry capture and dashboard data model
