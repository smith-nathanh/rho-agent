"""Observability processor that wraps agent event streams."""

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from ..core.agent import AgentEvent
from .config import ObservabilityConfig
from .context import TelemetryContext, TurnContext, ToolExecutionContext
from .exporters.base import Exporter, NoOpExporter
from .exporters.sqlite import create_exporter


class ObservabilityProcessor:
    """Wraps agent event streams to capture telemetry data.

    This processor intercepts AgentEvent objects as they flow through
    the agent, extracting metrics and sending them to the configured
    exporter without modifying the events themselves.

    Usage:
        processor = ObservabilityProcessor(config, context)
        await processor.start_session()

        # For each turn
        async for event in processor.wrap_turn(agent.run_turn(user_input), user_input):
            handle_event(event)

        await processor.end_session()
    """

    def __init__(
        self,
        config: ObservabilityConfig,
        context: TelemetryContext,
        exporter: Exporter | None = None,
    ) -> None:
        """Initialize the processor.

        Args:
            config: Observability configuration.
            context: Telemetry context for this session.
            exporter: Optional custom exporter. If not provided, one is created from config.
        """
        self._config = config
        self._context = context
        self._exporter = exporter or create_exporter(config)

        # Current turn state
        self._current_turn: TurnContext | None = None
        self._pending_tool: ToolExecutionContext | None = None

        # Metrics
        self._turn_input_tokens = 0
        self._turn_output_tokens = 0
        self._turn_reasoning_tokens = 0

        # Session lifecycle state for idempotent runtime reuse.
        self._session_started = False
        self._session_ended = False

    @property
    def context(self) -> TelemetryContext:
        """Get the telemetry context."""
        return self._context

    @property
    def exporter(self) -> Exporter:
        """Get the exporter."""
        return self._exporter

    async def start_session(self) -> None:
        """Start the telemetry session.

        This call is idempotent for a processor instance.
        """
        if self._session_started:
            return
        if self._session_ended:
            return
        await self._exporter.start_session(self._context)
        self._session_started = True

    async def end_session(self, status: str = "completed") -> None:
        """End the telemetry session.

        Args:
            status: Final session status ('completed', 'error', 'cancelled').
        """
        if not self._session_started:
            return
        if self._session_ended:
            return
        self._context.end_session(status)
        await self._exporter.end_session(self._context)
        self._session_ended = True

    async def wrap_turn(
        self,
        events: AsyncIterator[AgentEvent],
        user_input: str = "",
    ) -> AsyncIterator[AgentEvent]:
        """Wrap an agent turn's event stream to capture telemetry.

        This method:
        1. Creates a turn record at the start
        2. Tracks tool executions (tool_start -> tool_end pairs)
        3. Captures token usage from turn_complete events
        4. Yields all events unchanged

        Args:
            events: The agent's event stream from run_turn().
            user_input: The user's input for this turn.

        Yields:
            AgentEvent objects, unchanged from the source.
        """
        # Start new turn
        turn_id = self._context.start_turn()
        self._current_turn = TurnContext(
            turn_id=turn_id,
            session_id=self._context.session_id,
            turn_index=self._context.current_turn_index,
            user_input=user_input,
        )
        self._turn_input_tokens = 0
        self._turn_output_tokens = 0
        self._turn_reasoning_tokens = 0

        await self._exporter.start_turn(self._current_turn, user_input)

        try:
            async for event in events:
                # Process event for telemetry
                await self._process_event(event)

                # Yield unchanged
                yield event

                # Handle turn completion
                if event.type in ("turn_complete", "cancelled", "error"):
                    break

        finally:
            # End the turn
            if self._current_turn:
                self._current_turn.input_tokens = self._turn_input_tokens
                self._current_turn.output_tokens = self._turn_output_tokens
                self._current_turn.reasoning_tokens = self._turn_reasoning_tokens
                self._current_turn.end()
                await self._exporter.end_turn(self._current_turn)
                self._context.end_turn()
                self._current_turn = None

    async def _process_event(self, event: AgentEvent) -> None:
        """Process an event for telemetry capture."""
        if event.type == "tool_start":
            # Start tracking a tool execution
            self._pending_tool = ToolExecutionContext(
                turn_id=self._current_turn.turn_id if self._current_turn else "",
                tool_name=event.tool_name or "",
                arguments=event.tool_args or {} if self._config.capture.tool_arguments else {},
            )
            self._context.record_tool_call()
            await self._exporter.increment_tool_call(self._context.session_id)

        elif event.type == "tool_end":
            # Complete the tool execution
            if self._pending_tool:
                self._pending_tool.end(success=True)
                if self._config.capture.tool_results:
                    self._pending_tool.result = event.tool_result
                await self._exporter.record_tool_execution(self._pending_tool)
                self._pending_tool = None

        elif event.type == "tool_blocked":
            # Tool was blocked by user
            if self._pending_tool:
                self._pending_tool.end(success=False, error="Blocked by user")
                await self._exporter.record_tool_execution(self._pending_tool)
                self._pending_tool = None

        elif event.type == "turn_complete":
            # Extract token usage
            if event.usage:
                # Usage contains cumulative totals, we want the delta
                total_input = event.usage.get("total_input_tokens", 0)
                total_output = event.usage.get("total_output_tokens", 0)
                total_reasoning = event.usage.get("total_reasoning_tokens", 0)

                # Calculate delta from session totals
                delta_input = total_input - self._context.total_input_tokens
                delta_output = total_output - self._context.total_output_tokens
                delta_reasoning = total_reasoning - self._context.total_reasoning_tokens

                self._turn_input_tokens = delta_input
                self._turn_output_tokens = delta_output
                self._turn_reasoning_tokens = delta_reasoning

                # Update session totals
                self._context.record_tokens(delta_input, delta_output, delta_reasoning)

                # Extract context_size
                context_size = event.usage.get("context_size", 0)
                self._context.context_size = context_size
                if self._current_turn:
                    self._current_turn.context_size = context_size

        elif event.type == "error":
            # Record error in pending tool if any
            if self._pending_tool:
                self._pending_tool.end(success=False, error=event.content)
                await self._exporter.record_tool_execution(self._pending_tool)
                self._pending_tool = None


def create_processor(
    config: ObservabilityConfig | None = None,
    team_id: str | None = None,
    project_id: str | None = None,
    model: str = "",
    profile: str = "readonly",
) -> ObservabilityProcessor | None:
    """Create an observability processor from configuration.

    This is a convenience function that handles config loading and
    context creation.

    Args:
        config: Explicit config (takes precedence).
        team_id: Team ID (used with from_env if no config).
        project_id: Project ID (used with from_env if no config).
        model: Model being used.
        profile: Capability profile name.

    Returns:
        ObservabilityProcessor if observability is enabled, None otherwise.
    """
    if config is None:
        config = ObservabilityConfig.from_env(team_id=team_id, project_id=project_id)

    if not config.enabled or not config.tenant:
        return None

    context = TelemetryContext.from_config(config, model=model, profile=profile)

    return ObservabilityProcessor(config, context)
