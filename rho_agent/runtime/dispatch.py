"""Background dispatch helpers for many-agent orchestration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .cancellation import CancellationToken
from .run import run_prompt
from .protocol import Runtime
from .types import EventHandler, RunResult


@dataclass
class AgentHandle:
    """Handle for a background-dispatched agent run."""

    runtime: Runtime
    prompt: str
    task: asyncio.Task[RunResult]
    token: CancellationToken | None = None

    def cancel(self, reason: str = "requested") -> None:
        """Request cooperative cancellation."""
        if self.token:
            self.token.cancel(reason=reason)
        if self.runtime.observability:
            self.runtime.observability.context.metadata["cancel_source"] = "programmatic"
            self.runtime.observability.context.metadata["cancel_reason"] = reason
        self.runtime.agent.request_cancel()

    def done(self) -> bool:
        """Return True if run has completed."""
        return self.task.done()

    async def wait(self) -> RunResult:
        """Wait for completion and return run result."""
        return await self.task

    @property
    def status(self) -> str:
        """Best-effort status for the dispatched run."""
        if not self.task.done():
            return "running"
        if self.task.cancelled():
            return "cancelled"
        exc = self.task.exception()
        if exc:
            return "error"
        return self.task.result().status


def dispatch_prompt(
    runtime: Runtime,
    prompt: str,
    *,
    on_event: EventHandler | None = None,
    token: CancellationToken | None = None,
) -> AgentHandle:
    """Dispatch a prompt in background and return a handle."""
    task = asyncio.create_task(run_prompt(runtime, prompt, on_event=on_event))
    return AgentHandle(runtime=runtime, prompt=prompt, task=task, token=token)
