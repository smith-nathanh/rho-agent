---
title: Tools
description: Tool system shape and how handlers are exposed to the model.
order: 7
---

rho-agent tools use a handler pattern.

Each tool defines:

- `name`
- `description`
- JSON-schema `parameters`
- `handle()` implementation

## Categories

- Shell: command execution
- File inspection: read/grep/list/glob
- File edits: write/edit (profile-dependent)
- Databases: sqlite/postgres/mysql/oracle/vertica
- Integrations: Excel reader, Azure DevOps (when env vars are set)

## Notes

- Tool availability is constrained by profile.
- Database mutation access depends on profile/tool config.
- Prefer explicit verification commands after tool actions.
