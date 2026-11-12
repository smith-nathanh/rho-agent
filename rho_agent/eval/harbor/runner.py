"""Entry point for running rho-agent inside Harbor containers."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from importlib import metadata
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from rho_agent.client.litellm_client import LiteLLMClient
from rho_agent.core import Agent, AgentConfig, Session
from rho_agent.core.events import AgentEvent, CompletionGate
from rho_agent.core.state import State
from rho_agent.export.atif import trace_to_atif
from rho_agent.permissions import PermissionProfile
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


def format_tool_call(name: str, args: dict[str, Any] | None) -> str:
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


# Minimum seconds between rewrites of trajectory.json during a run.
TRAJECTORY_WRITE_INTERVAL = 20.0


def _agent_version() -> str:
    try:
        return metadata.version("rho-agent")
    except metadata.PackageNotFoundError:
        return "unknown"


CONFIRM_TOKEN = "CONFIRM_DONE"
CONFIRM_PROMPT = (
    "Before finalizing, verify your solution:\n"
    "1. If test files exist, run them with the appropriate test runner\n"
    "2. Check actual test output - exit code 0 alone doesn't mean tests passed\n"
    "3. Confirm ALL requirements are met, including any performance/accuracy thresholds\n"
    "4. Re-read the task instructions and verify each requirement\n"
    "5. Confirm every file or output the task asks for exists at the required path\n\n"
    "If everything passes verification, reply with exactly {token}. "
    "Otherwise, fix issues first."
)


async def run_reviewer_phase(
    instruction: str,
    event_trace: list[AgentEvent],
    session: Session,
    client: LiteLLMClient,
    max_iterations: int,
) -> None:
    """Run post-execution review with optional revision loop.

    Args:
        instruction: Original task instruction.
        event_trace: Full event trace from the actor phase.
        session: The actor session (used for revision turns).
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
        review_content, _ = prepare_prompt(
            reviewer_prompt,
            {
                "task_instruction": instruction,
                "agent_trace": formatted_trace,
            },
        )

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
            f"A reviewer found issues with your work:\n\n{feedback}\n\nPlease address these issues."
        )

        # Run actor revision turn and collect events for next review
        revision_events: list[AgentEvent] = []

        async def collect_revision(
            event: AgentEvent, _events: list[AgentEvent] = revision_events
        ) -> None:
            _events.append(event)
            if event.type == "text" and event.content:
                print(event.content, end="", flush=True)
            elif event.type == "tool_start":
                print(f"\n{format_tool_call(event.tool_name, event.tool_args)}", file=sys.stderr)

        await session.run(revision_prompt, on_event=collect_revision)

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
    system_prompt, _ = prepare_prompt(
        prompt,
        {
            "platform": "Linux",
            "home_dir": str(Path.home()),
            "working_dir": working_dir,
        },
    )

    # Create client from environment
    # LiteLLM uses model names like "openai/gpt-5-mini" or "anthropic/claude-3-5-sonnet"
    model = os.environ.get("RHO_AGENT_MODEL") or os.environ.get("OPENAI_MODEL", "openai/gpt-5-mini")
    # Normally unset: LiteLLM then reads the provider's own variable
    # (OPENAI_API_KEY, ANTHROPIC_API_KEY, ...), which works for any provider.
    api_key = os.environ.get("RHO_AGENT_API_KEY")
    temperature = (
        float(os.environ["RHO_AGENT_TEMPERATURE"])
        if "RHO_AGENT_TEMPERATURE" in os.environ
        else None
    )
    reasoning_effort = os.environ.get("RHO_AGENT_REASONING_EFFORT")
    chunk_timeout = float(os.environ.get("RHO_AGENT_CHUNK_TIMEOUT", "180.0"))
    initial_timeout = float(os.environ.get("RHO_AGENT_INITIAL_TIMEOUT", "600.0"))
    cost_ceiling_usd = float(os.environ.get("RHO_AGENT_COST_CEILING_USD", "0.0"))
    max_wall_seconds = float(os.environ.get("RHO_AGENT_MAX_WALL_SECONDS", "0.0"))
    max_call_seconds = float(os.environ.get("RHO_AGENT_MAX_CALL_SECONDS", "1200.0"))
    logs_dir = Path(os.environ.get("RHO_AGENT_LOGS_DIR", "/logs/agent"))

    client = LiteLLMClient(
        model=model,
        api_key=api_key,
        temperature=temperature,
        reasoning_effort=reasoning_effort,
        chunk_timeout=chunk_timeout,
        initial_timeout=initial_timeout,
    )

    # Build agent with unrestricted profile
    agent = Agent(
        AgentConfig(
            system_prompt=system_prompt,
            model=model,
            profile="unrestricted",
            working_dir=working_dir,
            auto_approve=True,
        )
    )

    # Replace registry with unrestricted tools
    profile = PermissionProfile.unrestricted(working_dir=working_dir)
    profile.bash_only = bash_only
    # Unset keeps the default: image viewing on, except in bash-only mode.
    view_image = os.environ.get("RHO_AGENT_VIEW_IMAGE", "").strip()
    if view_image:
        profile.view_image = view_image not in ("0", "false", "no", "off")
    # Benchmark runs measure a single agent; no child agents.
    profile.delegate = False
    from rho_agent.permissions.factory import ToolFactory

    factory = ToolFactory(profile)
    agent._registry = factory.create_registry(working_dir=working_dir)

    # Log available tools for debugging
    tool_names = [h.name for h in agent.registry._handlers.values()]
    print(f"[rho-agent] Tools: {tool_names}", file=sys.stderr)

    # Create session with LiteLLM client (multi-provider support in containers)
    # The trace is appended event by event, so a killed trial still has one.
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
        state = State(trace_path=logs_dir / "trace.jsonl")
    except OSError:
        state = State()
    session = Session(agent, client=client, state=state)
    # Compaction settings come from AgentConfig, which reads
    # RHO_AGENT_AUTO_COMPACT, RHO_AGENT_COMPACT_THRESHOLD and
    # RHO_AGENT_COMPACT_AT_TOKENS; the default is the model's own threshold.
    # Hard caps are enforced inside the agent loop, between every step.
    if cost_ceiling_usd > 0:
        session.max_cost_usd = cost_ceiling_usd
    if max_wall_seconds > 0:
        session.max_wall_seconds = max_wall_seconds
    # A single model call that runs this long is abandoned and retried.
    if max_call_seconds > 0:
        session.max_call_seconds = max_call_seconds

    # trajectory.json (ATIF) is derived from trace.jsonl. It is rewritten
    # periodically during the run so a killed trial still has one.
    last_trajectory_write = 0.0

    def write_trajectory(force: bool = False) -> None:
        nonlocal last_trajectory_write
        trace_path = state.trace_path
        if trace_path is None or not trace_path.exists():
            return
        now = time.monotonic()
        if not force and now - last_trajectory_write < TRAJECTORY_WRITE_INTERVAL:
            return
        last_trajectory_write = now
        try:
            trajectory = trace_to_atif(
                trace_path,
                session_id=session.id,
                agent_version=_agent_version(),
                model_name=model,
            )
            if not trajectory["steps"]:
                return  # ATIF needs at least one step; nothing to record yet
            target = logs_dir / "trajectory.json"
            tmp = target.with_name("trajectory.json.tmp")
            tmp.write_text(json.dumps(trajectory, indent=2))
            os.replace(tmp, target)
        except Exception as e:  # never let bookkeeping break the run
            print(f"[trajectory write failed: {e}]", file=sys.stderr)

    try:
        # Set when a cap, error or cancel ended a turn; later phases are skipped
        # so the trial ends promptly and the verifier grades what exists.
        stopped = False

        # Track events for reviewer (full trace, not just text output)
        event_trace: list[AgentEvent] = []

        async def run_turn(prompt_text: str) -> str:
            nonlocal stopped
            text_content = ""

            def _write_tokens() -> None:
                """Write tokens/cost incrementally to mounted path (survives process kill)."""
                (logs_dir / "tokens.json").write_text(
                    json.dumps(
                        {
                            "input": session.state.usage["input_tokens"],
                            "output": session.state.usage["output_tokens"],
                            "cached": session.state.usage["cached_tokens"],
                            "reasoning": session.state.usage["reasoning_tokens"],
                            "cost_usd": session.state.usage["cost_usd"],
                        }
                    )
                )

            async def on_event(event: AgentEvent) -> None:
                nonlocal text_content
                event_trace.append(event)
                if event.type == "text" and event.content:
                    text_content += event.content
                    print(event.content, end="", flush=True)
                elif event.type == "tool_start":
                    print(
                        f"\n{format_tool_call(event.tool_name, event.tool_args)}", file=sys.stderr
                    )
                elif event.type == "tool_end":
                    if os.environ.get("RHO_AGENT_DEBUG"):
                        result_preview = (
                            event.tool_result[:200] + "..."
                            if event.tool_result and len(event.tool_result) > 200
                            else event.tool_result
                        )
                        print(f"[Result: {result_preview}]", file=sys.stderr)
                    _write_tokens()
                    write_trajectory()
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
                elif event.type == "completion_gate":
                    print("\n[completion gate]", file=sys.stderr)
                elif event.type in ("retry", "max_turns", "budget_exceeded"):
                    print(f"\n[{event.type}: {event.content}]", file=sys.stderr)
                elif event.type == "turn_complete":
                    if event.usage:
                        cost = event.usage.get("total_cost_usd", 0.0)
                        reasoning = event.usage.get("total_reasoning_tokens", 0)
                        reasoning_str = f", reasoning={reasoning}" if reasoning else ""
                        print(
                            f"\n[Tokens: in={event.usage.get('total_input_tokens', 0)}, "
                            f"out={event.usage.get('total_output_tokens', 0)}{reasoning_str}, "
                            f"cost=${cost:.4f}]",
                            file=sys.stderr,
                        )
                    _write_tokens()

            result = await session.run(prompt_text, on_event=on_event)
            stopped = result.status in ("budget_exceeded", "error", "cancelled")

            return text_content

        # Completion gate: when the agent tries to finish, it is asked to verify
        # its work and must reply with the token (or use up the rounds).
        enable_confirm = os.environ.get("RHO_AGENT_CONFIRM_DONE", "1") == "1"
        max_confirm = int(os.environ.get("RHO_AGENT_CONFIRM_DONE_MAX", "3"))
        if enable_confirm and max_confirm > 0:
            session.completion_gate = CompletionGate(
                prompt=CONFIRM_PROMPT, token=CONFIRM_TOKEN, max_rounds=max_confirm
            )

        # Run initial actor turn
        await run_turn(instruction)
        session.completion_gate = None  # reviewer revisions are not gated

        # Reviewer phase (optional)
        enable_reviewer = os.environ.get("RHO_AGENT_ENABLE_REVIEWER") == "1"
        max_iterations = int(os.environ.get("RHO_AGENT_REVIEWER_MAX_ITERATIONS", "1"))

        if enable_reviewer and not stopped:
            await run_reviewer_phase(
                instruction=instruction,
                event_trace=event_trace,
                session=session,
                client=client,
                max_iterations=max_iterations,
            )
    finally:
        write_trajectory(force=True)

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
