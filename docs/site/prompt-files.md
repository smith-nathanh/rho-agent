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
initial_prompt: Investigate job {{ job_id }} on {{ cluster }}.
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
| `initial_prompt` | string | Optional initial user message sent to the agent automatically |

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
rho-agent main --prompt task.md --var cluster=prod --var log_path=/mnt/logs/123

# Variables file
rho-agent main --prompt task.md --vars-file vars.yaml
```

From the runtime API:

Variables are resolved at prompt load time before being passed to `create_runtime()`.

## Prompt precedence

The system prompt is resolved in this order:

1. `--system "..."` — overrides everything
2. `--prompt file.md` — loads the markdown body as system prompt
3. `~/.config/rho-agent/default-system.md` — custom default (if the file exists)
4. Built-in default system prompt

## Initial message precedence

The first user message sent to the agent is resolved in this order:

1. Positional argument: `rho-agent main --prompt task.md "focus on OOM errors"`
2. `initial_prompt` field from frontmatter
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
initial_prompt: >
  Connect to the {{ database }} database, list all tables,
  and analyze the {{ focus_area }} for optimization opportunities.
---

You are a database performance analyst.

Target database: {{ database }}

Analyze the schema with a focus on {{ focus_area }}. For each finding,
explain the issue, its performance impact, and a concrete recommendation.
```

```bash
rho-agent main --prompt schema-analysis.md \
  --var database=analytics \
  --var focus_area="query patterns"
```
