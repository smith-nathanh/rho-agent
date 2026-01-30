#!/usr/bin/env python3
"""Example: Using rho-agent programmatically from Python.

This demonstrates how to embed rho-agent in a Python script to:
1. Pass context and get a response (single turn)
2. Let the agent run autonomously with tools (multi-turn)

Example with custom directory and task:
    uv run python demo/programmatic_usage.py ~/some/project "Summarize the error handling"
"""

import asyncio
import os
import sys

from dotenv import load_dotenv

from rho_agent.client.model import ModelClient
from rho_agent.core.agent import Agent
from rho_agent.core.session import Session
from rho_agent.tools.handlers import (
    GlobHandler,
    GrepHandler,
    ListHandler,
    ReadHandler,
    WriteHandler,
)
from rho_agent.tools.registry import ToolRegistry


async def run_agent_with_tools(
    task: str,
    working_dir: str | None = None,
    output_file: str | None = None,
    auto_approve: bool = True,
) -> str:
    """Run the agent autonomously, letting it use tools as needed.

    Args:
        task: The task/question for the agent
        working_dir: Directory context for the agent to explore
        output_file: Optional path to write findings to
        auto_approve: Whether to auto-approve tool calls

    Returns:
        The agent's final text response
    """
    # Build system prompt with context
    system_prompt = "You are a research assistant. Investigate thoroughly using the available tools."
    if working_dir:
        system_prompt += f"\n\nWorking directory context: {working_dir}"

    # Initialize components
    session = Session(system_prompt=system_prompt)
    registry = ToolRegistry()

    # Register read-only tools
    registry.register(ReadHandler())
    registry.register(GrepHandler())
    registry.register(GlobHandler())
    registry.register(ListHandler())

    # Optionally register write for exporting findings
    if output_file:
        registry.register(WriteHandler())

    # Create model client (uses OPENAI_API_KEY and OPENAI_BASE_URL from env)
    client = ModelClient(
        model=os.environ.get("OPENAI_MODEL", "gpt-5-mini"),
        base_url=os.environ.get("OPENAI_BASE_URL"),
    )

    # Approval callback - auto-approve or always reject
    async def approval_callback(tool_name: str, tool_args: dict) -> bool:
        if auto_approve:
            return True
        print(f"Tool requires approval: {tool_name}({tool_args})")
        return False

    # Create agent
    agent = Agent(
        session=session,
        registry=registry,
        client=client,
        approval_callback=approval_callback,
    )

    # Run the agent and collect response
    response_text = ""
    tool_calls = []

    async for event in agent.run_turn(task):
        if event.type == "text" and event.content:
            response_text += event.content
            # Stream to console
            print(event.content, end="", flush=True)

        elif event.type == "tool_start":
            tool_calls.append(event.tool_name)
            print(f"\n[{event.tool_name}({event.tool_args})]", flush=True)

        elif event.type == "tool_end":
            meta = event.tool_metadata or {}
            print(f"  → {meta.get('summary', 'done')}", flush=True)

        elif event.type == "error":
            print(f"\nError: {event.content}", flush=True)

        elif event.type == "turn_complete":
            usage = event.usage or {}
            print(f"\n\n[{usage.get('total_input_tokens', 0)} in, {usage.get('total_output_tokens', 0)} out]")
            print(f"[{len(tool_calls)} tool calls: {', '.join(tool_calls)}]")

    return response_text


async def simple_query(context: str, question: str) -> str:
    """Simple query without tools - just pass context and get a response.

    Args:
        context: Context to include in the system prompt
        question: The question to ask

    Returns:
        The agent's response
    """
    system_prompt = f"""You are a helpful assistant.

Here is context to work with:
{context}
"""

    session = Session(system_prompt=system_prompt)
    registry = ToolRegistry()  # No tools registered

    client = ModelClient(
        model=os.environ.get("OPENAI_MODEL", "gpt-5-mini"),
        base_url=os.environ.get("OPENAI_BASE_URL"),
    )

    agent = Agent(session=session, registry=registry, client=client)

    response = ""
    async for event in agent.run_turn(question):
        if event.type == "text" and event.content:
            response += event.content

    return response


if __name__ == "__main__":
    load_dotenv()  # Load OPENAI_API_KEY from .env

    # Example: Let agent explore a directory with tools
    target_dir = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    task = sys.argv[2] if len(sys.argv) > 2 else "What does this project do? Give me a brief summary."

    print(f"=== Running agent on: {target_dir} ===\n")
    print(f"Task: {task}\n")
    print("=" * 60 + "\n")

    result = asyncio.run(
        run_agent_with_tools(
            task=task,
            working_dir=target_dir,
        )
    )

    print("\n" + "=" * 60)
    print("Done!")
