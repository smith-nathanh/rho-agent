---
description: "Reviewer prompt for Harbor TerminalBench evaluation revision loops"
variables:
  task_instruction:
    required: true
  agent_trace:
    required: true
---
Review the agent's attempt against the original task.

# Goal

Decide whether the agent successfully completed the task based on the instruction and execution trace.

# Original task

{{ task_instruction }}

# Agent trace

{{ agent_trace }}

# Review criteria

Approve only if the trace shows the agent completed the required work, not just that it claimed success.

Check for:
- Whether the final state satisfies the task requirements
- Whether the agent actually ran verification when verification was available
- Whether tool outputs indicate failures, missing steps, or contradictory evidence
- Whether files/commands/results shown in the trace support the claimed outcome

Do not require perfection beyond the task requirements. Minor style issues or alternate valid approaches are fine.

# Output format

Respond in exactly one of these forms:

`APPROVED`

or

`REVISION_NEEDED: <concise actionable feedback>`

If revision is needed, keep the feedback specific and execution-focused. State what is missing, incorrect, or insufficiently verified.
