"""Observability module for rho-agent.

Provides enterprise-grade observability with:
- Real-time monitoring of running agents
- Historical analysis and debugging
- Cost/token tracking
- Audit trail for compliance
- Multi-tenancy (team_id, project_id)

Basic Usage:
    from rho_agent.observability import (
        ObservabilityConfig,
        TelemetryContext,
        ObservabilityProcessor,
        create_processor,
    )

    # Create from environment/CLI args
    processor = create_processor(
        team_id="my-team",
        project_id="my-project",
        model="gpt-5-mini",
    )

    if processor:
        await processor.start_session()

        # Wrap agent event streams
        async for event in processor.wrap_turn(agent.run_turn(user_input), user_input):
            handle_event(event)

        await processor.end_session()

CLI Usage:
    # Enable telemetry via CLI flags
    rho-agent --team-id acme --project-id logs "analyze this error"

    # Or via environment variables
    export RHO_AGENT_TEAM_ID=acme
    export RHO_AGENT_PROJECT_ID=logs
    rho-agent "analyze this error"

    # Launch the dashboard
    rho-agent dashboard
"""

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
