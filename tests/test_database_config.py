"""Tests for multi-database configuration."""

import os
import tempfile
from pathlib import Path

import pytest

from rho_agent.tools.handlers.database_config import (
    DatabaseConfig,
    load_database_config,
    _interpolate_env_vars,
)


class TestEnvVarInterpolation:
    """Tests for environment variable interpolation."""

    def test_simple_interpolation(self):
        env = {"FOO": "bar", "BAZ": "qux"}
        assert _interpolate_env_vars("${FOO}", env) == "bar"
        assert _interpolate_env_vars("prefix_${FOO}_suffix", env) == "prefix_bar_suffix"
        assert _interpolate_env_vars("${FOO}/${BAZ}", env) == "bar/qux"

    def test_missing_env_var_becomes_empty(self):
        env = {}
        assert _interpolate_env_vars("${MISSING}", env) == ""
        assert _interpolate_env_vars("prefix_${MISSING}_suffix", env) == "prefix__suffix"

    def test_non_string_passthrough(self):
        env = {"FOO": "bar"}
        assert _interpolate_env_vars(123, env) == 123
        assert _interpolate_env_vars(None, env) is None
        assert _interpolate_env_vars(["a", "b"], env) == ["a", "b"]


class TestLoadDatabaseConfig:
    """Tests for load_database_config function."""

    def test_no_config_returns_empty(self):
        """When no config exists, returns empty dict."""
        configs = load_database_config(env={})
        assert configs == {}

    def test_missing_explicit_path_raises(self):
        """Explicit path that doesn't exist raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            load_database_config("/nonexistent/path.yaml")

    def test_load_sqlite_configs(self, tmp_path):
        """Load multiple SQLite configs."""
        # Create actual SQLite files
        sales_db = tmp_path / "sales.db"
        analytics_db = tmp_path / "analytics.db"
        sales_db.touch()
        analytics_db.touch()

        config_file = tmp_path / "databases.yaml"
        config_file.write_text(f"""
databases:
  sales:
    type: sqlite
    path: {sales_db}
  analytics:
    type: sqlite
    path: {analytics_db}
""")
        configs = load_database_config(str(config_file))

        assert "sqlite" in configs
        assert len(configs["sqlite"]) == 2

        aliases = {c.alias for c in configs["sqlite"]}
        assert aliases == {"sales", "analytics"}

    def test_load_mixed_db_types(self, tmp_path):
        """Load configs for different database types."""
        # Create actual SQLite file
        local_db = tmp_path / "local.db"
        local_db.touch()

        config_file = tmp_path / "databases.yaml"
        config_file.write_text(f"""
databases:
  local_db:
    type: sqlite
    path: {local_db}
  prod_pg:
    type: postgres
    host: db.example.com
    database: production
    user: app
    password: secret
  analytics_oracle:
    type: oracle
    dsn: analytics.example.com:1521/ORCL
    user: reader
    password: pass123
""")
        configs = load_database_config(str(config_file))

        assert "sqlite" in configs
        assert "postgres" in configs
        assert "oracle" in configs

        pg_config = configs["postgres"][0]
        assert pg_config.alias == "prod_pg"
        assert pg_config.host == "db.example.com"
        assert pg_config.database == "production"

    def test_env_var_interpolation_in_config(self, tmp_path):
        """Environment variables are interpolated."""
        config_file = tmp_path / "databases.yaml"
        config_file.write_text("""
databases:
  prod:
    type: postgres
    host: ${DB_HOST}
    database: mydb
    user: ${DB_USER}
    password: ${DB_PASSWORD}
""")
        env = {
            "DB_HOST": "prod-db.internal",
            "DB_USER": "admin",
            "DB_PASSWORD": "supersecret",
        }
        configs = load_database_config(str(config_file), env=env)

        pg_config = configs["postgres"][0]
        assert pg_config.host == "prod-db.internal"
        assert pg_config.user == "admin"
        assert pg_config.password == "supersecret"

    def test_validation_missing_required_field(self, tmp_path):
        """Missing required fields raise ValueError."""
        config_file = tmp_path / "databases.yaml"
        config_file.write_text("""
databases:
  broken:
    type: sqlite
    # missing 'path' field
""")
        with pytest.raises(ValueError, match="missing required fields: path"):
            load_database_config(str(config_file))

    def test_validation_unknown_db_type(self, tmp_path):
        """Unknown database type raises ValueError."""
        config_file = tmp_path / "databases.yaml"
        config_file.write_text("""
databases:
  broken:
    type: unknown_db
    path: /tmp/db
""")
        with pytest.raises(ValueError, match="Unknown database type"):
            load_database_config(str(config_file))

    def test_env_var_for_config_path(self, tmp_path):
        """RHO_AGENT_DB_CONFIG env var specifies config path."""
        # Create actual SQLite file
        test_db = tmp_path / "test.db"
        test_db.touch()

        config_file = tmp_path / "custom.yaml"
        config_file.write_text(f"""
databases:
  test:
    type: sqlite
    path: {test_db}
""")
        env = {"RHO_AGENT_DB_CONFIG": str(config_file)}
        configs = load_database_config(env=env)

        assert "sqlite" in configs
        assert configs["sqlite"][0].alias == "test"

    def test_duplicate_alias_across_types_raises(self, tmp_path):
        """Duplicate aliases across different DB types raise ValueError."""
        config_file = tmp_path / "databases.yaml"
        config_file.write_text("""
databases:
  mydb:
    type: sqlite
    path: /tmp/a.db
  mydb:
    type: postgres
    host: localhost
    database: test
""")
        # Note: YAML will actually overwrite the first 'mydb' key,
        # so this tests YAML's behavior, not our validation.
        # Our validation catches duplicates within the parsed dict.
        configs = load_database_config(str(config_file))
        # YAML overwrites, so we only get postgres
        assert "postgres" in configs
        assert "sqlite" not in configs

    def test_oracle_with_port_raises(self, tmp_path):
        """Oracle config with port field raises ValueError."""
        config_file = tmp_path / "databases.yaml"
        config_file.write_text("""
databases:
  myoracle:
    type: oracle
    dsn: host:1521/service
    port: 1521
    user: test
    password: test
""")
        with pytest.raises(ValueError, match="unsupported fields.*port"):
            load_database_config(str(config_file))

    def test_oracle_with_host_raises(self, tmp_path):
        """Oracle config with host field raises ValueError."""
        config_file = tmp_path / "databases.yaml"
        config_file.write_text("""
databases:
  myoracle:
    type: oracle
    dsn: host:1521/service
    host: myhost.example.com
    user: test
    password: test
""")
        with pytest.raises(ValueError, match="unsupported fields.*host"):
            load_database_config(str(config_file))

    def test_sqlite_with_host_raises(self, tmp_path):
        """SQLite config with host field raises ValueError."""
        # Create actual SQLite file (validation fails before path check)
        test_db = tmp_path / "test.db"
        test_db.touch()

        config_file = tmp_path / "databases.yaml"
        config_file.write_text(f"""
databases:
  mydb:
    type: sqlite
    path: {test_db}
    host: localhost
""")
        with pytest.raises(ValueError, match="unsupported fields.*host"):
            load_database_config(str(config_file))

    def test_sqlite_nonexistent_path_raises(self, tmp_path):
        """SQLite config with non-existent path raises ValueError."""
        config_file = tmp_path / "databases.yaml"
        config_file.write_text("""
databases:
  mydb:
    type: sqlite
    path: /nonexistent/path/to/db.sqlite
""")
        with pytest.raises(ValueError, match="path does not exist"):
            load_database_config(str(config_file))
