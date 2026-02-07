"""Database configuration loading with multi-database support."""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class DatabaseConfig:
    """Configuration for a single database connection."""

    alias: str  # User-defined name (e.g., "sales", "analytics")
    type: str  # Database type: sqlite, postgres, mysql, oracle, vertica

    # Connection params vary by type
    path: str | None = None  # SQLite
    host: str | None = None  # Others
    port: int | None = None
    database: str | None = None
    user: str | None = None
    password: str | None = None
    dsn: str | None = None  # Oracle


# Environment variable pattern for interpolation: ${VAR_NAME}
ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")


def _interpolate_env_vars(value: Any, env: dict[str, str]) -> Any:
    """Interpolate ${ENV_VAR} patterns in string values."""
    if not isinstance(value, str):
        return value

    def replace(match: re.Match[str]) -> str:
        var_name = match.group(1)
        return env.get(var_name, "")

    return ENV_VAR_PATTERN.sub(replace, value)


def _validate_config(alias: str, db_type: str, config: dict[str, Any]) -> None:
    """Validate required fields per database type."""
    required_fields: dict[str, list[str]] = {
        "sqlite": ["path"],
        "postgres": ["database"],
        "mysql": ["database"],
        "oracle": ["dsn"],
        "vertica": ["database"],
    }

    # Fields that are not allowed for specific database types
    disallowed_fields: dict[str, list[str]] = {
        "oracle": ["port", "host"],  # Oracle uses DSN which embeds host:port
        "sqlite": ["host", "port", "user", "password", "database", "dsn"],
    }

    if db_type not in required_fields:
        raise ValueError(
            f"Unknown database type '{db_type}' for '{alias}'. "
            f"Supported types: {', '.join(required_fields.keys())}"
        )

    missing = [f for f in required_fields[db_type] if not config.get(f)]
    if missing:
        raise ValueError(
            f"Database '{alias}' ({db_type}) missing required fields: {', '.join(missing)}"
        )

    # Check for disallowed fields
    if db_type in disallowed_fields:
        present_disallowed = [f for f in disallowed_fields[db_type] if config.get(f)]
        if present_disallowed:
            raise ValueError(
                f"Database '{alias}' ({db_type}) has unsupported fields: {', '.join(present_disallowed)}. "
                f"Oracle uses 'dsn' which includes host:port/service."
            )

    # Validate SQLite path exists
    if db_type == "sqlite":
        sqlite_path = Path(config["path"]).expanduser()
        if not sqlite_path.exists():
            raise ValueError(f"Database '{alias}' (sqlite) path does not exist: {config['path']}")


def _parse_database_entry(alias: str, entry: dict[str, Any], env: dict[str, str]) -> DatabaseConfig:
    """Parse a single database entry from config."""
    db_type = entry.get("type", "").lower()
    if not db_type:
        raise ValueError(f"Database '{alias}' missing required 'type' field")

    # Interpolate environment variables in all string values
    interpolated = {k: _interpolate_env_vars(v, env) for k, v in entry.items()}

    # Validate required fields
    _validate_config(alias, db_type, interpolated)

    return DatabaseConfig(
        alias=alias,
        type=db_type,
        path=interpolated.get("path"),
        host=interpolated.get("host"),
        port=int(interpolated["port"]) if interpolated.get("port") else None,
        database=interpolated.get("database"),
        user=interpolated.get("user"),
        password=interpolated.get("password"),
        dsn=interpolated.get("dsn"),
    )


def load_database_config(
    config_path: str | None = None, env: dict[str, str] | None = None
) -> dict[str, list[DatabaseConfig]]:
    """Load database configs grouped by type.

    Args:
        config_path: Path to YAML config file. If None, checks:
            1. RHO_AGENT_DB_CONFIG environment variable
            2. ~/.config/rho-agent/databases.yaml
        env: Environment variables for interpolation. Defaults to os.environ.

    Returns:
        Dictionary mapping database type to list of configs:
        {"sqlite": [DatabaseConfig, ...], "postgres": [...], ...}

    Raises:
        FileNotFoundError: If explicit config_path doesn't exist.
        ValueError: If config is invalid.
    """
    env = env or dict(os.environ)

    # Determine config path
    if config_path is None:
        config_path = env.get("RHO_AGENT_DB_CONFIG")

    if config_path is None:
        default_path = Path.home() / ".config" / "rho-agent" / "databases.yaml"
        if default_path.exists():
            config_path = str(default_path)

    if config_path is None:
        return {}  # No config found

    path = Path(config_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"Database config file not found: {path}")

    with open(path) as f:
        data = yaml.safe_load(f) or {}

    databases = data.get("databases", {})
    if not databases:
        return {}

    # Parse configs and group by type
    configs_by_type: dict[str, list[DatabaseConfig]] = {}
    all_aliases: set[str] = set()

    for alias, entry in databases.items():
        if not isinstance(entry, dict):
            raise ValueError(f"Database '{alias}' must be a dictionary")

        # Validate alias uniqueness across all database types
        if alias in all_aliases:
            raise ValueError(
                f"Duplicate database alias '{alias}'. "
                f"Aliases must be unique across all database types."
            )
        all_aliases.add(alias)

        config = _parse_database_entry(alias, entry, env)

        if config.type not in configs_by_type:
            configs_by_type[config.type] = []
        configs_by_type[config.type].append(config)

    return configs_by_type
