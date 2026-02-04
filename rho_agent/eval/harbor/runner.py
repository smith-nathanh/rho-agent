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
    RHO_AGENT_REASONING_EFFORT - Reasoning effort: "low", "medium", "high" (default: None)
    RHO_AGENT_CHUNK_TIMEOUT   - Streaming chunk timeout in seconds (default: 180)
    RHO_AGENT_INITIAL_TIMEOUT - Initial response timeout in seconds (default: 600)
    RHO_AGENT_COST_CEILING_USD - Max cost per task in USD, 0 = disabled (default: 0)
    OPENAI_API_KEY            - API key (required)
    RHO_AGENT_ENABLE_REVIEWER - Set to "1" to enable post-execution review
    RHO_AGENT_REVIEWER_MAX_ITERATIONS - Max review-revise loops (default: 1)
    RHO_AGENT_CONFIRM_DONE    - Set to "1" to require CONFIRM_DONE after actor completes
    RHO_AGENT_CONFIRM_DONE_MAX - Max confirm retries before proceeding (default: 3)
"""

import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from rho_agent.capabilities import CapabilityProfile
from rho_agent.capabilities.factory import ToolFactory
from rho_agent.client.litellm_client import LiteLLMClient
from rho_agent.core.agent import Agent, AgentEvent
from rho_agent.core.session import Session
from rho_agent.eval.harbor.trajectory import TrajectoryBuilder
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


def format_tool_call(name: str, args: dict | None) -> str:
    """Format tool name and args for console logging."""
    if not args:
        return f"[Tool: {name}]"
    # Create compact args summary
    args_preview = json.dumps(args, separators=(",", ":"))
    if len(args_preview) > 200:
        args_preview = args_preview[:200] + "..."
    return f"[Tool: {name}] {args_preview}"


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
    client: LiteLLMClient,
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
                print(f"\n{format_tool_call(event.tool_name, event.tool_args)}", file=sys.stderr)

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
    # LiteLLM uses model names like "openai/gpt-5-mini" or "anthropic/claude-3-5-sonnet"
    model = os.environ.get("RHO_AGENT_MODEL") or os.environ.get("OPENAI_MODEL", "openai/gpt-5-mini")
    api_key = os.environ.get("OPENAI_API_KEY")
    temperature = float(os.environ.get("RHO_AGENT_TEMPERATURE", "0.0"))
    reasoning_effort = os.environ.get("RHO_AGENT_REASONING_EFFORT")
    chunk_timeout = float(os.environ.get("RHO_AGENT_CHUNK_TIMEOUT", "180.0"))
    initial_timeout = float(os.environ.get("RHO_AGENT_INITIAL_TIMEOUT", "600.0"))
    cost_ceiling_usd = float(os.environ.get("RHO_AGENT_COST_CEILING_USD", "0.0"))

    client = LiteLLMClient(
        model=model,
        api_key=api_key,
        temperature=temperature,
        reasoning_effort=reasoning_effort,
        chunk_timeout=chunk_timeout,
        initial_timeout=initial_timeout,
    )

    # Determine context window for auto-compaction
    # Strip provider prefix for model detection (e.g., "openai/gpt-5-mini" -> "gpt-5-mini")
    model_name = model.split("/")[-1].lower()
    if "gpt-5" in model_name:
        context_window = 400_000  # GPT-5.x family
    elif "gpt-oss" in model_name:
        context_window = 128_000  # GPT-OSS-120B
    elif "claude" in model_name:
        context_window = 200_000  # Claude 3.x family
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
        enable_nudge=False,  # CONFIRM_DONE gate handles completion verification
    )

    # Set up observability to capture full tool traces to SQLite
    # Use /logs/agent/ which is mounted from host - survives process kill
    telemetry_db = os.environ.get("RHO_AGENT_TELEMETRY_DB", "/logs/agent/telemetry.db")
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

    # Create trajectory builder for ATIF output
    trajectory_builder = TrajectoryBuilder(model=model)

    try:
        # Track events for reviewer (full trace, not just text output)
        event_trace: list[AgentEvent] = []

        async def run_turn(prompt_text: str) -> str:
            # Check cost ceiling before calling agent - end gracefully to allow grading
            if cost_ceiling_usd > 0 and session.total_cost_usd >= cost_ceiling_usd:
                print(
                    f"\n[Cost ceiling reached: ${session.total_cost_usd:.2f} >= ${cost_ceiling_usd:.2f}]",
                    file=sys.stderr,
                )
                return ""

            events = agent.run_turn(prompt_text)
            if processor:
                events = processor.wrap_turn(events, prompt_text)

            text_content = ""
            turn_events: list[AgentEvent] = []
            async for event in events:
                event_trace.append(event)
                turn_events.append(event)
                if event.type == "text" and event.content:
                    text_content += event.content
                    print(event.content, end="", flush=True)
                elif event.type == "tool_start":
                    print(f"\n{format_tool_call(event.tool_name, event.tool_args)}", file=sys.stderr)
                elif event.type == "tool_end":
                    if os.environ.get("RHO_AGENT_DEBUG"):
                        result_preview = (
                            event.tool_result[:200] + "..."
                            if event.tool_result and len(event.tool_result) > 200
                            else event.tool_result
                        )
                        print(f"[Result: {result_preview}]", file=sys.stderr)
                    # Write tokens incrementally after each tool (survives timeout)
                    Path("/logs/agent/tokens.json").write_text(json.dumps({
                        "input": session.total_input_tokens,
                        "output": session.total_output_tokens,
                        "cached": session.total_cached_tokens,
                        "reasoning": session.total_reasoning_tokens,
                        "cost_usd": session.total_cost_usd,
                        "context_size": session.last_input_tokens,
                    }))
                elif event.type == "api_call_complete":
                    if os.environ.get("RHO_AGENT_DEBUG"):
                        usage = event.usage or {}
                        print(
                            f"[API call {usage.get('call_index')}: "
                            f"context={usage.get('input_tokens')}, "
                            f"out={usage.get('output_tokens')}, "
                            f"cost=${usage.get('cost_usd', 0):.4f}]",
                            file=sys.stderr,
                        )
                elif event.type == "compact_start":
                    print("\n[Compacting context...]", file=sys.stderr)
                elif event.type == "compact_end":
                    print(f"[{event.content}]", file=sys.stderr)
                elif event.type == "error":
                    print(f"\nError: {event.content}", file=sys.stderr)
                elif event.type == "turn_complete":
                    if event.usage:
                        cost = event.usage.get('total_cost_usd', 0.0)
                        reasoning = event.usage.get('total_reasoning_tokens', 0)
                        reasoning_str = f", reasoning={reasoning}" if reasoning else ""
                        print(
                            f"\n[Tokens: in={event.usage.get('total_input_tokens', 0)}, "
                            f"out={event.usage.get('total_output_tokens', 0)}{reasoning_str}, "
                            f"cost=${cost:.4f}]",
                            file=sys.stderr,
                        )
                    # Write tokens/cost incrementally to mounted path (survives process kill)
                    Path("/logs/agent/tokens.json").write_text(json.dumps({
                        "input": session.total_input_tokens,
                        "output": session.total_output_tokens,
                        "cached": session.total_cached_tokens,
                        "reasoning": session.total_reasoning_tokens,
                        "cost_usd": session.total_cost_usd,
                        "context_size": session.last_input_tokens,
                    }))

            # Build trajectory from this turn's events
            trajectory_builder.build_from_events(turn_events, user_input=prompt_text)

            return text_content

        # Run initial actor turn
        last_text = await run_turn(instruction)

        # Completion confirmation gate (Terminus-style)
        enable_confirm = os.environ.get("RHO_AGENT_CONFIRM_DONE", "1") == "1"
        max_confirm = int(os.environ.get("RHO_AGENT_CONFIRM_DONE_MAX", "3"))
        confirm_token = "CONFIRM_DONE"
        confirm_prompt = (
            "Before finalizing, verify your solution:\n"
            "1. If test files exist, run them with the appropriate test runner\n"
            "2. Check actual test output - exit code 0 alone doesn't mean tests passed\n"
            "3. Confirm ALL requirements are met, including any performance/accuracy thresholds\n"
            "4. Re-read the task instructions and verify each requirement\n\n"
            f"If everything passes verification, reply with exactly {confirm_token}. "
            "Otherwise, fix issues first."
        )

        if enable_confirm:
            for _ in range(max_confirm):
                confirm_text = await run_turn(confirm_prompt)
                if confirm_text.strip() == confirm_token:
                    break

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
            # Sync session tokens to processor context before ending.
            # The session tracks tokens incrementally from each API response,
            # but the processor only updates on turn_complete events which
            # may not fire if the agent times out.
            processor.context.total_input_tokens = session.total_input_tokens
            processor.context.total_output_tokens = session.total_output_tokens
            await processor.end_session()

        # Save ATIF trajectory for Harbor analysis
        trajectory_builder.save("/logs/agent/trajectory.json")

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
