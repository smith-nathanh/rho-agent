"""Observability module: telemetry capture, export, and dashboard."""

from __future__ import annotations

from .config import (
    ObservabilityConfig,
    TenantConfig,
    BackendConfig,
    CaptureConfig,
    SqliteBackendConfig,
    OtlpBackendConfig,
    DEFAULT_CONFIG_DIR,
    DEFAULT_CONFIG_FILE,
    DEFAULT_TELEMETRY_DB,
)
from .context import (
    TelemetryContext,
    TurnContext,
    ToolExecutionContext,
)
from .processor import (
    ObservabilityProcessor,
    create_processor,
)
from .exporters.base import (
    Exporter,
    NoOpExporter,
    CompositeExporter,
)
from .exporters.sqlite import (
    SQLiteExporter,
    create_exporter,
)
from .storage.sqlite import (
    TelemetryStorage,
    SessionSummary,
    SessionDetail,
    ToolStats,
    CostSummary,
)

__all__ = [
    # Config
    "ObservabilityConfig",
    "TenantConfig",
    "BackendConfig",
    "CaptureConfig",
    "SqliteBackendConfig",
    "OtlpBackendConfig",
    "DEFAULT_CONFIG_DIR",
    "DEFAULT_CONFIG_FILE",
    "DEFAULT_TELEMETRY_DB",
    # Context
    "TelemetryContext",
    "TurnContext",
    "ToolExecutionContext",
    # Processor
    "ObservabilityProcessor",
    "create_processor",
    # Exporters
    "Exporter",
    "NoOpExporter",
    "CompositeExporter",
    "SQLiteExporter",
    "create_exporter",
    # Storage
    "TelemetryStorage",
    "SessionSummary",
    "SessionDetail",
    "ToolStats",
    "CostSummary",
]
