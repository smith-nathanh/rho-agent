"""CLI entry point for ro-agent."""

import argparse
import asyncio
import os
import sys
from typing import Any

from dotenv import load_dotenv


# ANSI color codes
class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    CYAN = "\033[36m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    BLUE = "\033[34m"

from .client.model import ModelClient
from .core.agent import Agent, AgentEvent
from .core.session import Session
from .tools.registry import ToolRegistry
from .tools.handlers.shell import ShellHandler


DEFAULT_SYSTEM_PROMPT = """\
You are a research assistant that helps inspect logs, files, and databases.
You have access to shell commands for investigating issues.
You are read-only - you cannot modify files or execute destructive commands.
Be thorough in your investigation and provide clear summaries of what you find.
"""


class ApprovalHandler:
    """Handles command approval prompts."""

    def __init__(self, auto_approve: bool = False) -> None:
        self.auto_approve = auto_approve
        self.always_allow = False

    async def check_approval(self, tool_name: str, tool_args: dict[str, Any]) -> bool:
        """Prompt user for approval. Returns True if approved."""
        if self.auto_approve or self.always_allow:
            return True

        cmd = tool_args.get("command", str(tool_args))
        print(f"\n{Colors.YELLOW}[Approve? {tool_name}: {cmd}]{Colors.RESET}")
        print(f"{Colors.YELLOW}[y]es / [n]o / [a]lways allow:{Colors.RESET} ", end="", flush=True)

        try:
            response = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False

        if response in ("a", "always"):
            self.always_allow = True
            return True
        return response in ("y", "yes")


def create_registry(working_dir: str | None = None) -> ToolRegistry:
    """Create and configure the tool registry."""
    registry = ToolRegistry()
    registry.register(ShellHandler(working_dir=working_dir))
    return registry


async def run_interactive(session: Session, agent: Agent) -> None:
    """Run an interactive REPL session."""
    print("ro-agent interactive mode. Type 'exit' or Ctrl+C to quit.\n")

    while True:
        try:
            user_input = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break

        if not user_input:
            continue

        if user_input.lower() in ("exit", "quit"):
            print("Goodbye!")
            break

        # Run the turn and print events
        async for event in agent.run_turn(user_input):
            handle_event(event)


def handle_event(event: AgentEvent) -> None:
    """Handle an agent event by printing to console."""
    if event.type == "text":
        # LLM response - default color
        print(event.content, end="", flush=True)

    elif event.type == "tool_start":
        # Command being run - cyan
        cmd = event.tool_args.get("command", "") if event.tool_args else ""
        if cmd:
            print(f"\n{Colors.CYAN}▶ {event.tool_name}: {cmd}{Colors.RESET}", flush=True)
        else:
            print(f"\n{Colors.CYAN}▶ {event.tool_name}{Colors.RESET}", flush=True)

    elif event.type == "tool_end":
        # Command result - dim
        result = event.tool_result or ""
        if len(result) > 500:
            result = result[:500] + "...(truncated)"
        print(f"{Colors.DIM}{result}{Colors.RESET}\n", flush=True)

    elif event.type == "tool_blocked":
        print(f"{Colors.RED}✗ Command rejected{Colors.RESET}\n", flush=True)

    elif event.type == "turn_complete":
        usage = event.usage or {}
        print(f"\n{Colors.DIM}[{usage.get('total_input_tokens', 0)} in, "
              f"{usage.get('total_output_tokens', 0)} out]{Colors.RESET}\n", flush=True)

    elif event.type == "error":
        print(f"\n{Colors.RED}[Error: {event.content}]{Colors.RESET}\n", file=sys.stderr, flush=True)


async def run_single(session: Session, agent: Agent, prompt: str) -> None:
    """Run a single prompt and exit."""
    async for event in agent.run_turn(prompt):
        handle_event(event)


def main() -> None:
    """Main entry point."""
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="ro-agent: A read-only research assistant"
    )
    parser.add_argument(
        "prompt",
        nargs="?",
        help="Single prompt to run (omit for interactive mode)",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("OPENAI_MODEL", "gpt-4o"),
        help="Model to use (default: gpt-4o, or OPENAI_MODEL env var)",
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("OPENAI_BASE_URL"),
        help="API base URL for vLLM or other OpenAI-compatible endpoints",
    )
    parser.add_argument(
        "--system",
        default=DEFAULT_SYSTEM_PROMPT,
        help="System prompt for the agent",
    )
    parser.add_argument(
        "--working-dir",
        help="Working directory for shell commands",
    )
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="Automatically approve all tool calls (no prompts)",
    )

    args = parser.parse_args()

    # Set up components
    session = Session(system_prompt=args.system)
    registry = create_registry(working_dir=args.working_dir)
    client = ModelClient(
        model=args.model,
        base_url=args.base_url,
    )

    # Set up approval handler
    approval_handler = ApprovalHandler(auto_approve=args.auto_approve)

    agent = Agent(
        session=session,
        registry=registry,
        client=client,
        approval_callback=approval_handler.check_approval,
    )

    # Run
    if args.prompt:
        asyncio.run(run_single(session, agent, args.prompt))
    else:
        asyncio.run(run_interactive(session, agent))


if __name__ == "__main__":
    main()
