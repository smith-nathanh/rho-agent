"""Factory for creating telemetry storage backends."""

from __future__ import annotations

from ..config import ObservabilityConfig
from .protocol import TelemetryStore


def create_storage(config: ObservabilityConfig) -> TelemetryStore:
    """Create a storage backend based on configuration.

    Routes based on ``config.backend.type``:
    - ``"sqlite"`` → TelemetryStorage (default)
    - ``"postgres"`` → PostgresTelemetryStore (requires psycopg[pool])
    """
    backend_type = config.backend.type

    if backend_type == "postgres":
        from .postgres import PostgresTelemetryStore

        pg = config.backend.postgres
        return PostgresTelemetryStore(
            dsn=pg.dsn,
            min_size=pg.min_connections,
            max_size=pg.max_connections,
        )

    # Default: SQLite
    from .sqlite import TelemetryStorage

    return TelemetryStorage(config.backend.sqlite.path)
