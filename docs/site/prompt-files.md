---
title: Prompt Files
description: Markdown prompt templates with YAML frontmatter, variable substitution, and initial prompts.
order: 6
---

Prompt files are markdown documents with optional YAML frontmatter. The markdown body becomes the system prompt, with Jinja2 template variables substituted at load time. This makes it easy to define repeatable, parameterized agent tasks.

## Format

```markdown
---
description: Investigate a failed job
variables:
  cluster:
    required: true
  log_path:
    required: true
  job_id:
    default: "unknown"
---

You are debugging a failed job on {{ cluster }}.

Log location: {{ log_path }}

Start by reading the log file and identifying error patterns.
```

## Frontmatter fields

| Field | Type | Description |
|---|---|---|
| `description` | string | Human-readable description of the prompt's purpose |
| `variables` | dict | Variable definitions (see below) |

## Variable definitions

Variables can be defined in two formats:

**Full format** with explicit `required` and `default` fields:

```yaml
variables:
  cluster:
    required: true
  job_id:
    default: "unknown"
```

**Short format** where the value is the default:

```yaml
variables:
  job_id: "unknown"
```

Required variables without a value cause an error at load time.

## Variable substitution

Variables use Jinja2 syntax (`{{ variable_name }}`) and are substituted in both the markdown body (system prompt) and the `initial_prompt` field.

### Providing variables

From the CLI:

```bash
# Individual variables
rho-agent main --system-prompt task.md --var cluster=prod --var log_path=/mnt/logs/123

# Variables file
rho-agent main --system-prompt task.md --vars-file vars.yaml
```

From the Python API:

Variables are resolved at prompt load time when constructing an `AgentConfig` via the `AgentConfig.vars` field.

## Prompt precedence

The system prompt is resolved in this order:

1. `--system-prompt file.md` — loads the markdown body as system prompt
2. `~/.config/rho-agent/default.md` — custom default (if the file exists)
3. Built-in default system prompt

## Initial message precedence

The first user message sent to the agent is resolved in this order:

1. `--prompt "..."` — explicit prompt text for one-shot mode
2. Positional argument: `rho-agent main "focus on OOM errors"`
3. Neither — agent starts in interactive mode waiting for input

## Example

A prompt file for database schema analysis:

```markdown
---
description: Analyze database schema and suggest optimizations
variables:
  database:
    required: true
  focus_area:
    default: "indexes"
---

You are a database performance analyst.

Target database: {{ database }}

Analyze the schema with a focus on {{ focus_area }}. For each finding,
explain the issue, its performance impact, and a concrete recommendation.
```

```bash
rho-agent main --system-prompt schema-analysis.md \
  --var database=analytics \
  --var focus_area="query patterns"
```
