#!/usr/bin/env python3
"""Example: programmatic usage via rho_agent.runtime.

Example:
    uv run python demo/programmatic_usage.py ~/some/project "Summarize the error handling"
"""

import asyncio
import os
import sys

from dotenv import load_dotenv

from rho_agent.runtime import (
    RuntimeOptions,
    create_runtime,
    run_prompt,
)


async def run_agent_with_tools(
    task: str,
    working_dir: str | None = None,
    auto_approve: bool = True,
    team_id: str | None = None,
    project_id: str | None = None,
) -> str:
    """Run the agent autonomously with profile-based tools."""
    system_prompt = (
        "You are a research assistant. Investigate thoroughly using the available tools."
    )
    if working_dir:
        system_prompt += f"\n\nWorking directory context: {working_dir}"

    options = RuntimeOptions(
        working_dir=working_dir,
        profile="developer",
        auto_approve=auto_approve,
        team_id=team_id,
        project_id=project_id,
        telemetry_metadata={
            "source": "demo_programmatic_usage",
            "dispatch_kind": "single",
        },
    )
    runtime = create_runtime(system_prompt, options=options)
    tool_calls = []

    status = "completed"
    await runtime.start()
    try:
        result = await run_prompt(runtime, task)
        status = result.status
    except Exception:
        status = "error"
        raise
    finally:
        await runtime.close(status)
    for event in result.events:
        if event.type == "text" and event.content:
            print(event.content, end="", flush=True)
        elif event.type == "tool_start":
            tool_calls.append(event.tool_name or "unknown")
            print(f"\n[{event.tool_name}({event.tool_args})]", flush=True)
        elif event.type == "tool_end":
            meta = event.tool_metadata or {}
            print(f"  → {meta.get('summary', 'done')}", flush=True)
        elif event.type == "error":
            print(f"\nError: {event.content}", flush=True)
    usage = result.usage
    print(
        f"\n\n[{usage.get('total_input_tokens', 0)} in, {usage.get('total_output_tokens', 0)} out]"
    )
    print(f"[{len(tool_calls)} tool calls: {', '.join(tool_calls)}]")
    print(f"[status: {result.status}]")
    if runtime.observability and runtime.observability.context:
        print(
            "[telemetry session: "
            f"{runtime.observability.context.session_id} "
            f"team={runtime.observability.context.team_id} "
            f"project={runtime.observability.context.project_id}]"
        )
    return result.text


if __name__ == "__main__":
    load_dotenv()  # Load OPENAI_API_KEY from .env

    target_dir = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    task = (
        sys.argv[2] if len(sys.argv) > 2 else "What does this project do? Give me a brief summary."
    )
    team_id = os.environ.get("RHO_AGENT_TEAM_ID")
    project_id = os.environ.get("RHO_AGENT_PROJECT_ID")

    print(f"=== Running agent on: {target_dir} ===\n")
    print(f"Task: {task}\n")
    print("=" * 60 + "\n")

    result = asyncio.run(
        run_agent_with_tools(
            task=task,
            working_dir=target_dir,
            team_id=team_id,
            project_id=project_id,
        )
    )

    print("\n" + "=" * 60)
    print("Done!")
