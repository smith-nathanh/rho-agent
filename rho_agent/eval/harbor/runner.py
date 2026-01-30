"""Entry point for running rho-agent inside Harbor containers.

This module runs rho-agent with unrestricted eval-mode tools using the
capability profile system. The container provides sandboxing, so tool-level
restrictions are unnecessary.

Usage:
    python -m rho_agent.eval.harbor.runner "<instruction>" [working_dir] [--bash-only]

Options:
    --bash-only           Only provide bash tool (no Read, Grep, etc.)

Environment variables:
    OPENAI_MODEL              - Model to use (default: gpt-5-mini)
    OPENAI_BASE_URL           - API base URL (default: OpenAI)
    RHO_AGENT_MODEL           - Override for OPENAI_MODEL
    RHO_AGENT_BASE_URL        - Override for OPENAI_BASE_URL
    RHO_AGENT_SERVICE_TIER    - OpenAI service tier: "flex" for lower cost (default: None)
    OPENAI_API_KEY            - API key (required)
    RHO_AGENT_ENABLE_REVIEWER - Set to "1" to enable post-execution review
    RHO_AGENT_REVIEWER_MAX_ITERATIONS - Max review-revise loops (default: 1)
"""

import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from rho_agent.capabilities import CapabilityProfile
from rho_agent.capabilities.factory import ToolFactory
from rho_agent.client.model import ModelClient
from rho_agent.core.agent import Agent, AgentEvent
from rho_agent.core.session import Session
from rho_agent.observability import ObservabilityConfig, CaptureConfig, TenantConfig, create_processor
from rho_agent.prompts import load_prompt, prepare_prompt

# Load .env file - try multiple locations
# 1. Current directory
# 2. rho-agent package root (where this file lives, up 4 levels)
# 3. /rho-agent (Harbor container mount point)
_pkg_root = Path(__file__).parent.parent.parent.parent
for env_path in [Path.cwd() / ".env", _pkg_root / ".env", Path("/rho-agent/.env")]:
    if env_path.exists():
        load_dotenv(env_path)
        break

_EVAL_PROMPT = Path(__file__).parent.parent.parent / "prompts" / "eval_terminal.md"
_REVIEWER_PROMPT = Path(__file__).parent.parent.parent / "prompts" / "eval_reviewer.md"

# Reviewer system prompt (text-only, no tools)
_REVIEWER_SYSTEM = """\
You are a code reviewer evaluating whether an AI agent successfully completed a task.
Be concise and direct. Focus on whether the task requirements were met."""


def format_event_trace(events: list[AgentEvent]) -> str:
    """Format agent events into a readable trace for the reviewer."""
    lines = []
    for event in events:
        if event.type == "text":
            lines.append(f"[Agent Response]\n{event.content}")
        elif event.type == "tool_start":
            args_str = json.dumps(event.tool_args, indent=2) if event.tool_args else "{}"
            lines.append(f"[Tool Call: {event.tool_name}]\nArguments:\n{args_str}")
        elif event.type == "tool_end":
            # Truncate long results
            result = event.tool_result or ""
            if len(result) > 2000:
                result = result[:2000] + "\n... (truncated)"
            lines.append(f"[Tool Result: {event.tool_name}]\n{result}")
        elif event.type == "error":
            lines.append(f"[Error]\n{event.content}")
    return "\n\n".join(lines)


async def auto_approve(tool_name: str, tool_args: dict) -> bool:
    """Auto-approve all tool calls in eval mode."""
    return True


async def run_reviewer_phase(
    instruction: str,
    event_trace: list[AgentEvent],
    agent: Agent,
    client: "ModelClient",
    max_iterations: int,
) -> None:
    """Run post-execution review with optional revision loop.

    Args:
        instruction: Original task instruction.
        event_trace: Full event trace from the actor phase.
        agent: The actor agent (used for revision turns).
        client: Model client for reviewer calls.
        max_iterations: Maximum review-revise iterations (0 = review only).
    """
    # Load reviewer prompt template
    reviewer_prompt = load_prompt(_REVIEWER_PROMPT)

    for iteration in range(max_iterations + 1):  # +1 for initial review
        print(f"\n[Reviewer: iteration {iteration + 1}]", file=sys.stderr)

        # Format event trace for reviewer
        formatted_trace = format_event_trace(event_trace)

        # Build review prompt from template
        review_content, _ = prepare_prompt(reviewer_prompt, {
            "task_instruction": instruction,
            "agent_trace": formatted_trace,
        })

        # Create reviewer session (text-only, no tools)
        reviewer_session = Session(system_prompt=_REVIEWER_SYSTEM)
        reviewer_session.add_user_message(review_content)

        # Get review using raw completion (no tools)
        messages = [
            {"role": "system", "content": _REVIEWER_SYSTEM},
            {"role": "user", "content": review_content},
        ]
        review_text, _ = await client.complete(messages)

        # Log verdict preview
        preview = review_text[:100] + "..." if len(review_text) > 100 else review_text
        print(f"[Reviewer verdict: {preview}]", file=sys.stderr)

        # Parse verdict
        review_stripped = review_text.strip()
        if review_stripped.startswith("APPROVED"):
            print("[Reviewer: APPROVED]", file=sys.stderr)
            return

        if iteration >= max_iterations:
            print("[Reviewer: max iterations reached]", file=sys.stderr)
            return

        # Extract revision feedback
        if "REVISION_NEEDED:" in review_text:
            feedback = review_text.split("REVISION_NEEDED:", 1)[1].strip()
        else:
            feedback = review_text

        # Run revision with actor
        print(f"[Revision needed: {feedback[:100]}...]", file=sys.stderr)
        revision_prompt = (
            f"A reviewer found issues with your work:\n\n{feedback}\n\n"
            f"Please address these issues."
        )

        # Run actor revision turn and collect events for next review
        revision_events: list[AgentEvent] = []
        async for event in agent.run_turn(revision_prompt):
            revision_events.append(event)
            if event.type == "text" and event.content:
                print(event.content, end="", flush=True)
            elif event.type == "tool_start":
                print(f"\n[Tool: {event.tool_name}]", file=sys.stderr)

        # Use new trace for next review iteration
        event_trace = revision_events

        print()  # Newline after revision output


async def run_task(instruction: str, working_dir: str = "/app", bash_only: bool = False) -> None:
    """Run rho-agent on a TerminalBench task.

    Args:
        instruction: The task description/instruction.
        working_dir: Working directory for shell commands (default: /app).
        bash_only: If True, only provide bash tool (no Read, Grep, etc.).
    """
    # Load and render eval prompt template
    prompt = load_prompt(_EVAL_PROMPT)
    system_prompt, _ = prepare_prompt(prompt, {
        "platform": "Linux",
        "home_dir": str(Path.home()),
        "working_dir": working_dir,
    })
    session = Session(system_prompt=system_prompt)

    # Use eval profile - unrestricted, no approval required
    profile = CapabilityProfile.eval(working_dir=working_dir)
    profile.bash_only = bash_only
    factory = ToolFactory(profile)
    registry = factory.create_registry(working_dir=working_dir)

    # Log available tools for debugging
    tool_names = [h.name for h in registry._handlers.values()]
    print(f"[rho-agent] Tools: {tool_names}", file=sys.stderr)

    # Create client from environment
    model = os.environ.get("RHO_AGENT_MODEL") or os.environ.get("OPENAI_MODEL", "gpt-5-mini")
    base_url = os.environ.get("RHO_AGENT_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
    api_key = os.environ.get("OPENAI_API_KEY")
    service_tier = os.environ.get("RHO_AGENT_SERVICE_TIER")

    client = ModelClient(
        model=model,
        base_url=base_url,
        api_key=api_key,
        service_tier=service_tier,
    )

    # Determine context window for auto-compaction
    model_lower = model.lower()
    if "gpt-5" in model_lower:
        context_window = 400_000  # GPT-5.x family
    elif "gpt-oss" in model_lower:
        context_window = 128_000  # GPT-OSS-120B
    else:
        context_window = 128_000  # conservative default

    # Create agent with auto-approval (container is sandbox)
    agent = Agent(
        session=session,
        registry=registry,
        client=client,
        approval_callback=auto_approve,
        auto_compact=True,
        context_window=context_window,
        enable_nudge=True,  # Push agent to keep working if it stops prematurely
    )

    # Set up observability to capture full tool traces to SQLite
    telemetry_db = os.environ.get("RHO_AGENT_TELEMETRY_DB", "/tmp/telemetry.db")
    obs_config = ObservabilityConfig(
        enabled=True,
        tenant=TenantConfig(team_id="eval", project_id="harbor"),
        capture=CaptureConfig(tool_arguments=True, tool_results=True),
    )
    obs_config.backend.sqlite.path = telemetry_db
    processor = create_processor(config=obs_config, model=model, profile="eval")

    # run_turn handles the full tool loop internally:
    # model → tool → model → tool → ... → text-only response → done.
    # The model stops calling tools when it's finished, then the
    # runner exits and Harbor runs verification.
    if processor:
        await processor.start_session()
    try:
        events = agent.run_turn(instruction)
        if processor:
            events = processor.wrap_turn(events, instruction)

        # Track events for reviewer (full trace, not just text output)
        event_trace: list[AgentEvent] = []

        async for event in events:
            event_trace.append(event)
            if event.type == "text" and event.content:
                print(event.content, end="", flush=True)
            elif event.type == "tool_start":
                print(f"\n[Tool: {event.tool_name}]", file=sys.stderr)
            elif event.type == "tool_end":
                if os.environ.get("RHO_AGENT_DEBUG"):
                    result_preview = (
                        event.tool_result[:200] + "..."
                        if event.tool_result and len(event.tool_result) > 200
                        else event.tool_result
                    )
                    print(f"[Result: {result_preview}]", file=sys.stderr)
            elif event.type == "compact_start":
                print("\n[Compacting context...]", file=sys.stderr)
            elif event.type == "compact_end":
                print(f"[{event.content}]", file=sys.stderr)
            elif event.type == "error":
                print(f"\nError: {event.content}", file=sys.stderr)
            elif event.type == "turn_complete":
                if event.usage:
                    print(
                        f"\n[Tokens: in={event.usage.get('total_input_tokens', 0)}, "
                        f"out={event.usage.get('total_output_tokens', 0)}]",
                        file=sys.stderr,
                    )

        # Reviewer phase (optional)
        enable_reviewer = os.environ.get("RHO_AGENT_ENABLE_REVIEWER") == "1"
        max_iterations = int(os.environ.get("RHO_AGENT_REVIEWER_MAX_ITERATIONS", "1"))

        if enable_reviewer:
            await run_reviewer_phase(
                instruction=instruction,
                event_trace=event_trace,
                agent=agent,
                client=client,
                max_iterations=max_iterations,
            )
    finally:
        if processor:
            await processor.end_session()

    print()  # Final newline


def main() -> None:
    """CLI entry point."""
    if len(sys.argv) < 2:
        print(
            "Usage: python -m rho_agent.eval.harbor.runner '<instruction>' [working_dir] [--bash-only]",
            file=sys.stderr,
        )
        sys.exit(1)

    # Parse --bash-only flag
    bash_only = "--bash-only" in sys.argv
    args = [a for a in sys.argv[1:] if a != "--bash-only"]

    instruction = args[0]
    working_dir = args[1] if len(args) > 1 else "/app"

    asyncio.run(run_task(instruction, working_dir, bash_only=bash_only))


if __name__ == "__main__":
    main()
