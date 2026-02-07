"""Observability processor that wraps agent event streams."""

import uuid
from collections.abc import AsyncIterator

from ..core.agent import AgentEvent
from .config import ObservabilityConfig
from .context import TelemetryContext, TurnContext, ToolExecutionContext
from .exporters.base import Exporter
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
        self._pending_tools: dict[str, ToolExecutionContext] = {}
        self._pending_tool_order: list[str] = []

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
        self._pending_tools = {}
        self._pending_tool_order = []

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
            tool_execution = ToolExecutionContext(
                turn_id=self._current_turn.turn_id if self._current_turn else "",
                tool_name=event.tool_name or "",
                arguments=event.tool_args or {} if self._config.capture.tool_arguments else {},
            )
            pending_key = event.tool_call_id or tool_execution.execution_id or str(uuid.uuid4())
            self._pending_tools[pending_key] = tool_execution
            self._pending_tool_order.append(pending_key)
            self._context.record_tool_call()
            await self._exporter.increment_tool_call(self._context.session_id)

        elif event.type == "tool_end":
            # Complete the tool execution
            pending_tool = self._pop_pending_tool(event.tool_call_id)
            if pending_tool:
                pending_tool.end(success=True)
                if self._config.capture.tool_results:
                    pending_tool.result = event.tool_result
                await self._exporter.record_tool_execution(pending_tool)

        elif event.type == "tool_blocked":
            # Tool was blocked by user
            pending_tool = self._pop_pending_tool(event.tool_call_id)
            if pending_tool:
                pending_tool.end(success=False, error="Blocked by user")
                await self._exporter.record_tool_execution(pending_tool)

        elif event.type == "api_call_complete":
            if event.usage and self._current_turn:
                input_tokens = event.usage.get("input_tokens", 0)
                output_tokens = event.usage.get("output_tokens", 0)
                reasoning_tokens = event.usage.get("reasoning_tokens", 0)

                self._turn_input_tokens += input_tokens
                self._turn_output_tokens += output_tokens
                self._turn_reasoning_tokens += reasoning_tokens
                self._context.record_tokens(input_tokens, output_tokens, reasoning_tokens)

                # Latency isn't currently emitted by AgentEvent usage.
                await self._exporter.record_model_call(
                    self._current_turn.turn_id,
                    input_tokens,
                    output_tokens,
                    latency_ms=0,
                )

        elif event.type == "turn_complete":
            # Extract token usage
            if event.usage:
                # Usage contains cumulative totals, we want the delta
                total_input = event.usage.get("total_input_tokens", 0)
                total_output = event.usage.get("total_output_tokens", 0)
                total_reasoning = event.usage.get("total_reasoning_tokens", 0)

                # If turn_complete includes usage not captured in per-call events,
                # add only the remainder to avoid double counting.
                remainder_input = max(0, total_input - self._context.total_input_tokens)
                remainder_output = max(0, total_output - self._context.total_output_tokens)
                remainder_reasoning = max(0, total_reasoning - self._context.total_reasoning_tokens)

                self._turn_input_tokens += remainder_input
                self._turn_output_tokens += remainder_output
                self._turn_reasoning_tokens += remainder_reasoning
                self._context.record_tokens(
                    remainder_input,
                    remainder_output,
                    remainder_reasoning,
                )

                # Extract context_size
                context_size = event.usage.get("context_size", 0)
                self._context.context_size = context_size
                if self._current_turn:
                    self._current_turn.context_size = context_size

        elif event.type == "error":
            # Record error in all pending tools
            pending_tools = self._drain_pending_tools()
            for pending_tool in pending_tools:
                pending_tool.end(success=False, error=event.content)
                await self._exporter.record_tool_execution(pending_tool)

    def _pop_pending_tool(self, tool_call_id: str | None) -> ToolExecutionContext | None:
        """Pop a pending tool by call ID, falling back to FIFO order."""
        if tool_call_id and tool_call_id in self._pending_tools:
            tool = self._pending_tools.pop(tool_call_id)
            if tool_call_id in self._pending_tool_order:
                self._pending_tool_order.remove(tool_call_id)
            return tool

        while self._pending_tool_order:
            key = self._pending_tool_order.pop(0)
            tool = self._pending_tools.pop(key, None)
            if tool:
                return tool
        return None

    def _drain_pending_tools(self) -> list[ToolExecutionContext]:
        """Drain and return all pending tools."""
        pending: list[ToolExecutionContext] = []
        while self._pending_tool_order:
            key = self._pending_tool_order.pop(0)
            tool = self._pending_tools.pop(key, None)
            if tool:
                pending.append(tool)
        # Catch any keys not in order list.
        if self._pending_tools:
            pending.extend(self._pending_tools.values())
            self._pending_tools = {}
        return pending


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
