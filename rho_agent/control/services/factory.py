"""Factory for creating control transports based on observability config."""

from __future__ import annotations

from rho_agent.observability.config import ObservabilityConfig

from .transport import ControlTransport


def create_transport(config: ObservabilityConfig) -> ControlTransport:
    """Create the appropriate control transport based on backend type.

    - ``"postgres"`` → ``PostgresSignalTransport`` (cross-node via DB)
    - anything else → ``LocalSignalTransport`` (filesystem signals)
    """
    if config.backend.type == "postgres" and config.backend.postgres.dsn:
        from .postgres_transport import PostgresSignalTransport

        return PostgresSignalTransport(dsn=config.backend.postgres.dsn)

    from .local_signal_transport import LocalSignalTransport

    return LocalSignalTransport()
