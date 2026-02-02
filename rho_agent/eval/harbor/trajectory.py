"""ATIF trajectory builder for Harbor eval compatibility.

Converts rho-agent's AgentEvent stream to ATIF (Agent Trajectory
Interchange Format) for use with Harbor's trajectory analysis tools.

ATIF Format:
    {
        "steps": [
            {
                "source": "user" | "agent",
                "message": str,  # for user/agent text
                "tool_calls": [...],  # for agent tool invocations
                "observations": [...],  # tool results
                "metrics": {...}  # token counts, cost
            },
            ...
        ],
        "metadata": {
            "model": str,
            "total_input_tokens": int,
            "total_output_tokens": int,
            "total_cached_tokens": int,
            "total_cost_usd": float,
            "extra": {  # optional, present when reasoning tokens used
                "total_reasoning_tokens": int
            }
        }
    }
"""

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rho_agent.core.agent import AgentEvent


@dataclass
class ToolCallEntry:
    """A tool call in ATIF format."""

    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ObservationEntry:
    """A tool result/observation in ATIF format."""

    source_call_id: str
    content: str
    metadata: dict[str, Any] | None = None


@dataclass
class Step:
    """A single step in the ATIF trajectory."""

    source: str  # "user" or "agent"
    message: str | None = None
    tool_calls: list[ToolCallEntry] = field(default_factory=list)
    observations: list[ObservationEntry] = field(default_factory=list)
    metrics: dict[str, Any] | None = None


class TrajectoryBuilder:
    """Builds ATIF-compliant trajectories from AgentEvent streams.

    Usage:
        builder = TrajectoryBuilder(model="gpt-5-mini")
        builder.add_user_step("What is 2+2?")
        builder.build_from_events(events)
        builder.save(path / "trajectory.json")
    """

    def __init__(self, model: str | None = None) -> None:
        """Initialize the trajectory builder.

        Args:
            model: Model name for metadata.
        """
        self._model = model
        self._steps: list[Step] = []
        self._total_input_tokens: int = 0
        self._total_output_tokens: int = 0
        self._total_cached_tokens: int = 0
        self._total_reasoning_tokens: int = 0
        self._total_cost_usd: float = 0.0

    def add_user_step(self, message: str) -> None:
        """Add a user input as a step.

        Args:
            message: The user's input text.
        """
        self._steps.append(Step(source="user", message=message))

    def build_from_events(self, events: list[AgentEvent], user_input: str | None = None) -> None:
        """Convert AgentEvents to ATIF steps.

        Processes events from a single turn and appends steps to the trajectory.

        Args:
            events: List of AgentEvent objects from a turn.
            user_input: Optional user input to add before agent events.
        """
        if user_input:
            self.add_user_step(user_input)

        # Accumulate agent content for current step
        current_text = ""
        current_tool_calls: list[ToolCallEntry] = []
        pending_observations: list[ObservationEntry] = []

        # Map tool_start events to their call IDs for matching with tool_end
        tool_call_ids: dict[str, str] = {}  # tool_name -> call_id (simplified)

        for event in events:
            if event.type == "text" and event.content:
                current_text += event.content

            elif event.type == "tool_start":
                # Generate a call ID for this tool invocation
                call_id = str(uuid.uuid4())[:8]
                tool_name = event.tool_name or "unknown"
                tool_call_ids[tool_name] = call_id

                current_tool_calls.append(
                    ToolCallEntry(
                        call_id=call_id,
                        name=tool_name,
                        arguments=event.tool_args or {},
                    )
                )

            elif event.type == "tool_end":
                tool_name = event.tool_name or "unknown"
                # Match with the most recent tool_start for this name
                call_id = tool_call_ids.get(tool_name, str(uuid.uuid4())[:8])

                pending_observations.append(
                    ObservationEntry(
                        source_call_id=call_id,
                        content=event.tool_result or "",
                        metadata=event.tool_metadata,
                    )
                )

            elif event.type == "turn_complete":
                # Extract metrics from turn_complete
                if event.usage:
                    self._total_input_tokens = event.usage.get("total_input_tokens", 0)
                    self._total_output_tokens = event.usage.get("total_output_tokens", 0)
                    self._total_cached_tokens = event.usage.get("total_cached_tokens", 0)
                    self._total_reasoning_tokens = event.usage.get("total_reasoning_tokens", 0)
                    self._total_cost_usd = event.usage.get("total_cost_usd", 0.0)

        # Create agent step(s) from accumulated content
        if current_text or current_tool_calls:
            step = Step(
                source="agent",
                message=current_text if current_text else None,
                tool_calls=current_tool_calls,
                observations=pending_observations,
            )
            self._steps.append(step)

    def to_trajectory(self) -> dict[str, Any]:
        """Export as ATIF-compliant dict.

        Returns:
            Dictionary in ATIF format.
        """
        steps_data = []
        for step in self._steps:
            step_dict: dict[str, Any] = {"source": step.source}

            if step.message:
                step_dict["message"] = step.message

            if step.tool_calls:
                step_dict["tool_calls"] = [
                    {
                        "call_id": tc.call_id,
                        "name": tc.name,
                        "arguments": tc.arguments,
                    }
                    for tc in step.tool_calls
                ]

            if step.observations:
                step_dict["observations"] = [
                    {
                        "source_call_id": obs.source_call_id,
                        "content": obs.content,
                        **({"metadata": obs.metadata} if obs.metadata else {}),
                    }
                    for obs in step.observations
                ]

            if step.metrics:
                step_dict["metrics"] = step.metrics

            steps_data.append(step_dict)

        metadata: dict[str, Any] = {
            "model": self._model,
            "total_input_tokens": self._total_input_tokens,
            "total_output_tokens": self._total_output_tokens,
            "total_cached_tokens": self._total_cached_tokens,
            "total_cost_usd": self._total_cost_usd,
        }

        # Add reasoning tokens in extra field for ATIF compliance
        # Reasoning tokens are a subset of output_tokens, tracked separately for analysis
        if self._total_reasoning_tokens:
            metadata["extra"] = {
                "total_reasoning_tokens": self._total_reasoning_tokens,
            }

        return {
            "steps": steps_data,
            "metadata": metadata,
        }

    def save(self, path: Path | str) -> None:
        """Write trajectory to JSON file.

        Args:
            path: Output file path.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        trajectory = self.to_trajectory()
        path.write_text(json.dumps(trajectory, indent=2))
