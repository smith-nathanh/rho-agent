---
title: Profiles
description: Capability profiles that control shell access, file permissions, database modes, and approval behavior.
order: 8
---

Profiles define what an agent can and cannot do. They control shell execution mode, file write permissions, database mutation access, and tool approval requirements. Every agent runs under a profile — the default is `readonly`.

## Built-in profiles

### `readonly`

Safe research profile for inspecting production systems. No file writes, restricted shell, read-only database access.

| Capability | Setting |
|---|---|
| Shell | Restricted (allowlisted commands only) |
| File write | Off |
| Database | SELECT only |
| Approval | Dangerous tools require approval |

```bash
rho-agent main --profile readonly
```

### `developer`

Full development profile with file editing and unrestricted shell access. Database queries remain read-only by default.

| Capability | Setting |
|---|---|
| Shell | Unrestricted |
| File write | Full |
| Database | SELECT only |
| Approval | Granular (database tools require approval) |

```bash
rho-agent main --profile developer --working-dir ~/proj/myapp
```

### `eval`

Unrestricted profile for sandboxed environments. No restrictions, no approval prompts. Intended for containers running benchmarks or evaluations where the security boundary is the container itself.

| Capability | Setting |
|---|---|
| Shell | Unrestricted |
| File write | Full |
| Database | Full (mutations allowed) |
| Approval | None |

```bash
rho-agent main --profile eval
```

### `developer-bash-only`

Same capabilities as `developer`, but only registers the `bash` tool. File inspection and database tools are not available — the agent must use shell commands for everything.

## Custom profiles

Point `--profile` to a YAML file to define custom capabilities:

```bash
rho-agent main --profile path/to/my-profile.yaml
```

### YAML schema

```yaml
profile: my-custom-profile
description: "Description of this profile's purpose"

shell:
  mode: restricted | unrestricted

file_write:
  mode: off | create-only | full

database:
  mode: readonly | mutations

approval:
  mode: all | dangerous | granular | none
  required_tools:        # Used with granular mode
    - oracle
    - mysql
  dangerous_patterns:    # Patterns that trigger approval prompts
    - "rm -rf"
    - "DROP TABLE"

shell_timeout: 120       # Seconds (default: 120)
shell_working_dir: /app  # Default working directory
bash_only: false         # Only register bash tool
```

### Capability modes

**Shell modes**

| Mode | Behavior |
|---|---|
| `restricted` | Only allowlisted read-only commands. Redirects and destructive commands are blocked. |
| `unrestricted` | Any shell command is allowed. |

**File write modes**

| Mode | Behavior |
|---|---|
| `off` | No file writing tools available. |
| `create-only` | Can create new files but not overwrite existing ones. Blocks writes to sensitive paths. |
| `full` | Unrestricted file write and edit access. |

**Database modes**

| Mode | Behavior |
|---|---|
| `readonly` | Only SELECT queries. INSERT, UPDATE, DELETE, DROP, CREATE, ALTER, and TRUNCATE are blocked. |
| `mutations` | All SQL operations allowed. |

**Approval modes**

| Mode | Behavior |
|---|---|
| `all` | Every tool call requires user approval. |
| `dangerous` | Tools classified as dangerous require approval (bash, write, edit, database tools, delegate). |
| `granular` | Only tools listed in `required_tools` require approval. |
| `none` | No approval prompts. |

### Example: read-only with database approval

```yaml
profile: production-research
description: "Read-only access with explicit approval for database queries"

shell:
  mode: restricted

file_write:
  mode: off

database:
  mode: readonly

approval:
  mode: granular
  required_tools:
    - postgres
    - oracle
```
