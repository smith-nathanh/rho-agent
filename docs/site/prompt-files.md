---
title: Prompt Files
description: Template prompts with frontmatter variables and optional initial prompts.
order: 6
---

Prompt files are markdown documents with optional YAML frontmatter.

## Example

```markdown
---
description: Investigate a failed job
variables:
  cluster: { required: true }
  log_path: { required: true }
  job_id: { default: "unknown" }
initial_prompt: Investigate job {{ job_id }} on {{ cluster }}.
---

You are debugging a failed job on {{ cluster }}.

Log location: {{ log_path }}
```

## Run a prompt file

```bash
uv run rho-agent main --prompt examples/job-failure.md --var cluster=prod --var log_path=/mnt/logs/123
```

## Tips

- Keep prompts task-specific and explicit about constraints.
- Prefer deterministic checklists for eval and CI-like tasks.
