---
title: Runtime API
description: Programmatic Python interface for dispatching and managing agent runs.
order: 5
---

Use `rho_agent.runtime` when embedding agents in services, workers, or batch systems.

## Example

```python
import asyncio
from rho_agent import RuntimeOptions, close_runtime, create_runtime, run_prompt, start_runtime


async def main() -> None:
    runtime = create_runtime(
        "You are a research assistant.",
        options=RuntimeOptions(
            profile="developer",
            working_dir="/tmp/work",
            team_id="acme",
            project_id="incident-response",
            telemetry_metadata={"job_id": "job-123"},
        ),
    )
    status = "completed"
    await start_runtime(runtime)
    try:
        result = await run_prompt(runtime, "Analyze recent failures and summarize root causes.")
        status = result.status
        print(result.text)
    finally:
        await close_runtime(runtime, status)


asyncio.run(main())
```

## When to use API vs CLI

- Use API for high-volume dispatch and integration with existing systems.
- Use CLI for manual exploration and ad hoc investigations.
