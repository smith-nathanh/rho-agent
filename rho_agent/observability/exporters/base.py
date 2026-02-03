"""Base exporter interface for telemetry data."""

from abc import ABC, abstractmethod
from typing import Any

from ..context import TelemetryContext, TurnContext, ToolExecutionContext


class Exporter(ABC):
    """Abstract base class for telemetry exporters.

    Exporters are responsible for persisting telemetry data to a backend.
    They handle session lifecycle, turn tracking, and tool execution recording.
    """

    @abstractmethod
    async def start_session(self, context: TelemetryContext) -> None:
        """Called when a new session starts.

        Args:
            context: The telemetry context for the session.
        """
        pass

    @abstractmethod
    async def end_session(self, context: TelemetryContext) -> None:
        """Called when a session ends.

        Args:
            context: The telemetry context with final state.
        """
        pass

    @abstractmethod
    async def start_turn(self, turn: TurnContext, user_input: str = "") -> None:
        """Called when a new turn starts.

        Args:
            turn: The turn context.
            user_input: The user's input for this turn.
        """
        pass

    @abstractmethod
    async def end_turn(self, turn: TurnContext) -> None:
        """Called when a turn ends.

        Args:
            turn: The turn context with final token counts.
        """
        pass

    @abstractmethod
    async def record_model_call(
        self,
        turn_id: str,
        input_tokens: int,
        output_tokens: int,
        latency_ms: int,
    ) -> None:
        """Record a model API call within a turn.

        Args:
            turn_id: The turn this call belongs to.
            input_tokens: Number of input tokens.
            output_tokens: Number of output tokens.
            latency_ms: Latency in milliseconds.
        """
        pass

    @abstractmethod
    async def record_tool_execution(
        self,
        execution: ToolExecutionContext,
    ) -> None:
        """Record a tool execution.

        Args:
            execution: The tool execution context with results.
        """
        pass

    async def flush(self) -> None:
        """Flush any buffered data to the backend.

        Override this if your exporter buffers data.
        """
        pass

    async def close(self) -> None:
        """Clean up any resources.

        Override this if your exporter needs cleanup.
        """
        pass

    async def increment_tool_call(self, session_id: str) -> None:
        """Increment session tool call counter.

        Override this to persist tool counts incrementally.
        Default is no-op for backwards compatibility.
        """
        pass


class NoOpExporter(Exporter):
    """Exporter that does nothing. Used when observability is disabled."""

    async def start_session(self, context: TelemetryContext) -> None:
        pass

    async def end_session(self, context: TelemetryContext) -> None:
        pass

    async def start_turn(self, turn: TurnContext, user_input: str = "") -> None:
        pass

    async def end_turn(self, turn: TurnContext) -> None:
        pass

    async def record_model_call(
        self,
        turn_id: str,
        input_tokens: int,
        output_tokens: int,
        latency_ms: int,
    ) -> None:
        pass

    async def record_tool_execution(
        self,
        execution: ToolExecutionContext,
    ) -> None:
        pass


class CompositeExporter(Exporter):
    """Exporter that delegates to multiple exporters.

    Useful for sending data to multiple backends simultaneously.
    """

    def __init__(self, exporters: list[Exporter]) -> None:
        self._exporters = exporters

    async def start_session(self, context: TelemetryContext) -> None:
        for exporter in self._exporters:
            await exporter.start_session(context)

    async def end_session(self, context: TelemetryContext) -> None:
        for exporter in self._exporters:
            await exporter.end_session(context)

    async def start_turn(self, turn: TurnContext, user_input: str = "") -> None:
        for exporter in self._exporters:
            await exporter.start_turn(turn, user_input)

    async def end_turn(self, turn: TurnContext) -> None:
        for exporter in self._exporters:
            await exporter.end_turn(turn)

    async def record_model_call(
        self,
        turn_id: str,
        input_tokens: int,
        output_tokens: int,
        latency_ms: int,
    ) -> None:
        for exporter in self._exporters:
            await exporter.record_model_call(turn_id, input_tokens, output_tokens, latency_ms)

    async def record_tool_execution(
        self,
        execution: ToolExecutionContext,
    ) -> None:
        for exporter in self._exporters:
            await exporter.record_tool_execution(execution)

    async def increment_tool_call(self, session_id: str) -> None:
        for exporter in self._exporters:
            await exporter.increment_tool_call(session_id)

    async def flush(self) -> None:
        for exporter in self._exporters:
            await exporter.flush()

    async def close(self) -> None:
        for exporter in self._exporters:
            await exporter.close()
