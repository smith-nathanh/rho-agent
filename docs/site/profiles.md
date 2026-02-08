---
title: Profiles
description: Capability profiles and how to choose safe defaults.
order: 8
---

Profiles control shell access, file writes, and database mutation behavior.

| Profile | Shell | File Write | Database | Typical use |
| --- | --- | --- | --- | --- |
| `readonly` | Restricted allowlist | Off | SELECT only | Production-safe research |
| `developer` | Unrestricted | Full | SELECT only | Local development |
| `eval` | Unrestricted | Full | Full | Sandboxed benchmark runs |

## Commands

```bash
uv run rho-agent main --profile readonly
uv run rho-agent main --profile developer
uv run rho-agent main --profile eval
```

## Custom profiles

You can point `--profile` to a YAML file to define custom capabilities and approval requirements.
