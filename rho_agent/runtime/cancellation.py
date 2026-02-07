"""Cancellation primitives for programmatic dispatch."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class CancellationToken:
    """Cooperative cancellation token shared between controller and runtime."""

    reason: str | None = None
    cancelled_at: datetime | None = None
    _cancelled: bool = False

    def cancel(self, reason: str = "requested") -> None:
        """Mark token as cancelled (idempotent)."""
        if self._cancelled:
            return
        self._cancelled = True
        self.reason = reason
        self.cancelled_at = datetime.now(timezone.utc)

    def is_cancelled(self) -> bool:
        """Return True if cancellation was requested."""
        return self._cancelled
