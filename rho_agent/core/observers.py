"""State observers for live export and telemetry.

Observers implement the StateObserver protocol and are notified on every
state mutation. The trace.jsonl file written by State is the primary record;
observers are opt-in side channels for live export.
"""

from __future__ import annotations

from typing import Any

from .state import StateObserver


class TelemetryObserver:
    """StateObserver that bridges to the existing ObservabilityProcessor.

    Wraps existing exporter logic (SQLite, Postgres) so sessions can
    auto-attach telemetry when configured.
    """

    def __init__(
        self,
        *,
        team_id: str | None = None,
        project_id: str | None = None,
        observability_config: str | None = None,
        model: str = "",
        profile: str = "",
        session_id: str = "",
    ) -> None:
        self._processor = None
        self._team_id = team_id
        self._project_id = project_id
        self._observability_config = observability_config
        self._model = model
        self._profile = profile
        self._session_id = session_id

    def _ensure_processor(self) -> Any:
        """Lazily initialize the observability processor."""
        if self._processor is not None:
            return self._processor

        try:
            from ..observability.config import ObservabilityConfig
            from ..observability.context import TelemetryContext
            from ..observability.processor import ObservabilityProcessor

            config = ObservabilityConfig.load(
                config_path=self._observability_config,
                team_id=self._team_id,
                project_id=self._project_id,
            )
            if not config.enabled or not config.tenant:
                return None

            context = TelemetryContext.from_config(
                config, model=self._model, profile=self._profile
            )
            context.session_id = self._session_id
            self._processor = ObservabilityProcessor(config, context)
            return self._processor
        except Exception:
            return None

    def on_event(self, event: dict[str, Any]) -> None:
        """Receive a trace event from State. Fire-and-forget to exporter."""
        # The existing ObservabilityProcessor wraps agent event streams.
        # This observer receives raw trace events; it logs them for
        # future integration. For now, it's a placeholder that can be
        # extended to map trace events to the exporter API.
        pass

    @property
    def processor(self) -> Any:
        """Access the underlying ObservabilityProcessor (for start/end session)."""
        return self._ensure_processor()
