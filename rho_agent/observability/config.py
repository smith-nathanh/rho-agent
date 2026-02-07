"""Configuration for observability."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


# Default paths
DEFAULT_CONFIG_DIR = Path.home() / ".config" / "rho-agent"
DEFAULT_CONFIG_FILE = DEFAULT_CONFIG_DIR / "observability.yaml"
DEFAULT_TELEMETRY_DB = DEFAULT_CONFIG_DIR / "telemetry.db"


@dataclass
class TenantConfig:
    """Tenant identification for multi-tenancy."""

    team_id: str
    project_id: str


@dataclass
class SqliteBackendConfig:
    """SQLite backend configuration."""

    path: str = str(DEFAULT_TELEMETRY_DB)


@dataclass
class OtlpBackendConfig:
    """OTLP backend configuration."""

    endpoint: str = "http://localhost:4317"
    insecure: bool = True
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class BackendConfig:
    """Backend configuration."""

    type: str = "sqlite"  # "sqlite" or "otlp"
    sqlite: SqliteBackendConfig = field(default_factory=SqliteBackendConfig)
    otlp: OtlpBackendConfig = field(default_factory=OtlpBackendConfig)


@dataclass
class CaptureConfig:
    """What to capture in telemetry."""

    traces: bool = True
    metrics: bool = True
    tool_arguments: bool = True
    tool_results: bool = False  # Can be large, disabled by default


@dataclass
class ObservabilityConfig:
    """Main observability configuration."""

    enabled: bool = True
    tenant: TenantConfig | None = None
    backend: BackendConfig = field(default_factory=BackendConfig)
    capture: CaptureConfig = field(default_factory=CaptureConfig)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ObservabilityConfig":
        """Create config from dictionary (e.g., parsed YAML)."""
        obs_data = data.get("observability", data)

        # Parse tenant config
        tenant = None
        if "tenant" in obs_data:
            tenant_data = obs_data["tenant"]
            tenant = TenantConfig(
                team_id=tenant_data.get("team_id", ""),
                project_id=tenant_data.get("project_id", ""),
            )

        # Parse backend config
        backend = BackendConfig()
        if "backend" in obs_data:
            backend_data = obs_data["backend"]
            backend.type = backend_data.get("type", "sqlite")

            if "sqlite" in backend_data:
                sqlite_data = backend_data["sqlite"]
                path = sqlite_data.get("path", str(DEFAULT_TELEMETRY_DB))
                # Expand ~ in path
                backend.sqlite = SqliteBackendConfig(path=str(Path(path).expanduser()))

            if "otlp" in backend_data:
                otlp_data = backend_data["otlp"]
                backend.otlp = OtlpBackendConfig(
                    endpoint=otlp_data.get("endpoint", "http://localhost:4317"),
                    insecure=otlp_data.get("insecure", True),
                    headers=otlp_data.get("headers", {}),
                )

        # Parse capture config
        capture = CaptureConfig()
        if "capture" in obs_data:
            capture_data = obs_data["capture"]
            capture.traces = capture_data.get("traces", True)
            capture.metrics = capture_data.get("metrics", True)
            capture.tool_arguments = capture_data.get("tool_arguments", True)
            capture.tool_results = capture_data.get("tool_results", False)

        return cls(
            enabled=obs_data.get("enabled", True),
            tenant=tenant,
            backend=backend,
            capture=capture,
        )

    @classmethod
    def from_yaml(cls, path: str | Path) -> "ObservabilityConfig":
        """Load config from YAML file."""
        path = Path(path).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")

        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        return cls.from_dict(data or {})

    @classmethod
    def from_env(
        cls,
        team_id: str | None = None,
        project_id: str | None = None,
    ) -> "ObservabilityConfig":
        """Create config from environment variables and CLI arguments.

        CLI arguments take precedence over environment variables.
        """
        # Resolve team_id: CLI arg > env var
        resolved_team_id = team_id or os.getenv("RHO_AGENT_TEAM_ID")
        resolved_project_id = project_id or os.getenv("RHO_AGENT_PROJECT_ID")

        # If no tenant info provided, observability is disabled
        if not resolved_team_id or not resolved_project_id:
            return cls(enabled=False)

        tenant = TenantConfig(
            team_id=resolved_team_id,
            project_id=resolved_project_id,
        )

        # Check for config file
        config_path = os.getenv("RHO_AGENT_OBSERVABILITY_CONFIG")
        if config_path:
            config = cls.from_yaml(config_path)
            # Override tenant from CLI/env
            config.tenant = tenant
            return config

        # Check default config location
        if DEFAULT_CONFIG_FILE.exists():
            config = cls.from_yaml(DEFAULT_CONFIG_FILE)
            config.tenant = tenant
            return config

        # Use defaults with provided tenant
        return cls(
            enabled=True,
            tenant=tenant,
        )

    @classmethod
    def load(
        cls,
        config_path: str | None = None,
        team_id: str | None = None,
        project_id: str | None = None,
    ) -> "ObservabilityConfig":
        """Load config with precedence: explicit path > CLI args > env vars > defaults.

        Args:
            config_path: Explicit path to config file (highest precedence).
            team_id: Team ID from CLI argument.
            project_id: Project ID from CLI argument.

        Returns:
            Loaded observability config.
        """
        if config_path:
            config = cls.from_yaml(config_path)
            # Override tenant if provided via CLI
            if team_id or project_id:
                resolved_team_id = team_id or os.getenv("RHO_AGENT_TEAM_ID", "")
                resolved_project_id = project_id or os.getenv("RHO_AGENT_PROJECT_ID", "")
                if resolved_team_id and resolved_project_id:
                    config.tenant = TenantConfig(
                        team_id=resolved_team_id,
                        project_id=resolved_project_id,
                    )
            return config

        return cls.from_env(team_id=team_id, project_id=project_id)
