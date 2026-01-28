"""Tool factory for creating registries from capability profiles."""

import os
from pathlib import Path
from typing import Any

from ..tools.base import ToolHandler
from ..tools.registry import ToolRegistry
from . import (
    CapabilityProfile,
    DatabaseMode,
    FileWriteMode,
    ShellMode,
)


class ToolFactory:
    """Factory for creating tool registries from capability profiles.

    The factory instantiates and configures tools based on the profile's
    capability settings, handling mode-specific behavior transparently.
    """

    def __init__(self, profile: CapabilityProfile):
        """Initialize the factory with a capability profile.

        Args:
            profile: The capability profile defining tool configuration.
        """
        self.profile = profile

    def create_registry(
        self,
        working_dir: str | None = None,
        env: dict[str, str] | None = None,
    ) -> ToolRegistry:
        """Create a configured tool registry.

        Args:
            working_dir: Working directory for shell commands.
            env: Environment variables to check for database configuration.
                 Defaults to os.environ.

        Returns:
            A configured ToolRegistry with tools enabled per the profile.
        """
        registry = ToolRegistry()
        env = env or dict(os.environ)
        working_dir = working_dir or os.getcwd()

        # Register core tools (always available)
        self._register_core_tools(registry)

        # Register bash tool (mode depends on profile)
        self._register_bash_tool(registry, working_dir)

        # Register write/edit tools (if enabled)
        self._register_write_tools(registry)

        # Register database tools (if configured)
        self._register_database_tools(registry, env)

        # Register external service tools (if configured)
        self._register_external_services(registry, env)

        return registry

    def _register_core_tools(self, registry: ToolRegistry) -> None:
        """Register tools that are always available."""
        from ..tools.handlers.read import ReadHandler
        from ..tools.handlers.glob import GlobHandler
        from ..tools.handlers.grep import GrepHandler
        from ..tools.handlers.list import ListHandler
        from ..tools.handlers.read_excel import ReadExcelHandler

        registry.register(ReadHandler())
        registry.register(GlobHandler())
        registry.register(GrepHandler())
        registry.register(ListHandler())
        registry.register(ReadExcelHandler())

    def _register_bash_tool(self, registry: ToolRegistry, working_dir: str) -> None:
        """Register the bash tool with appropriate restrictions."""
        from ..tools.handlers.bash import BashHandler

        restricted = self.profile.shell == ShellMode.RESTRICTED
        requires_approval = self.profile.requires_tool_approval("bash")

        handler = BashHandler(
            restricted=restricted,
            working_dir=working_dir,
            timeout=self.profile.shell_timeout,
            requires_approval=requires_approval,
        )
        registry.register(handler)

    def _register_write_tools(self, registry: ToolRegistry) -> None:
        """Register write and edit tools if enabled."""
        from ..tools.handlers.write import WriteHandler
        from ..tools.handlers.edit import EditHandler

        if self.profile.file_write == FileWriteMode.OFF:
            return

        # Create-only mode: can create files but not overwrite
        # Full mode: can create, overwrite, and edit
        create_only = self.profile.file_write == FileWriteMode.CREATE_ONLY
        requires_write_approval = self.profile.requires_tool_approval("write")

        registry.register(WriteHandler(
            create_only=create_only,
            requires_approval=requires_write_approval,
        ))

        # Edit tool only available in FULL mode
        if self.profile.file_write == FileWriteMode.FULL:
            requires_edit_approval = self.profile.requires_tool_approval("edit")
            registry.register(EditHandler(requires_approval=requires_edit_approval))

    def _register_database_tools(
        self, registry: ToolRegistry, env: dict[str, str]
    ) -> None:
        """Register database tools that are configured via environment."""
        readonly = self.profile.database == DatabaseMode.READONLY

        # Oracle
        if env.get("ORACLE_DSN"):
            self._register_oracle(registry, readonly)

        # SQLite
        if env.get("SQLITE_DB"):
            self._register_sqlite(registry, readonly)

        # Vertica
        if env.get("VERTICA_HOST"):
            self._register_vertica(registry, readonly)

        # MySQL
        if env.get("MYSQL_HOST"):
            self._register_mysql(registry, readonly)

        # PostgreSQL
        if env.get("POSTGRES_HOST"):
            self._register_postgres(registry, readonly)

    def _register_oracle(self, registry: ToolRegistry, readonly: bool) -> None:
        """Register Oracle handler if available."""
        try:
            from ..tools.handlers.oracle import OracleHandler
            requires_approval = self.profile.requires_tool_approval("oracle")
            handler = OracleHandler(readonly=readonly, requires_approval=requires_approval)
            registry.register(handler)
        except ImportError:
            pass  # oracledb not installed

    def _register_sqlite(self, registry: ToolRegistry, readonly: bool) -> None:
        """Register SQLite handler."""
        from ..tools.handlers.sqlite import SqliteHandler
        requires_approval = self.profile.requires_tool_approval("sqlite")
        handler = SqliteHandler(readonly=readonly, requires_approval=requires_approval)
        registry.register(handler)

    def _register_vertica(self, registry: ToolRegistry, readonly: bool) -> None:
        """Register Vertica handler if available."""
        try:
            from ..tools.handlers.vertica import VerticaHandler
            requires_approval = self.profile.requires_tool_approval("vertica")
            handler = VerticaHandler(readonly=readonly, requires_approval=requires_approval)
            registry.register(handler)
        except ImportError:
            pass  # vertica-python not installed

    def _register_mysql(self, registry: ToolRegistry, readonly: bool) -> None:
        """Register MySQL handler if available."""
        try:
            from ..tools.handlers.mysql import MysqlHandler
            requires_approval = self.profile.requires_tool_approval("mysql")
            handler = MysqlHandler(readonly=readonly, requires_approval=requires_approval)
            registry.register(handler)
        except ImportError:
            pass  # mysql-connector-python not installed

    def _register_postgres(self, registry: ToolRegistry, readonly: bool) -> None:
        """Register PostgreSQL handler if available."""
        try:
            from ..tools.handlers.postgres import PostgresHandler
            requires_approval = self.profile.requires_tool_approval("postgres")
            handler = PostgresHandler(readonly=readonly, requires_approval=requires_approval)
            registry.register(handler)
        except ImportError:
            pass  # psycopg not installed

    def _register_external_services(
        self, registry: ToolRegistry, env: dict[str, str]
    ) -> None:
        """Register external service tools that are configured via environment."""
        # Azure DevOps - writable by default since agent needs to post findings
        # Set AZURE_DEVOPS_READONLY=true to disable mutations
        if env.get("AZURE_DEVOPS_ORG"):
            readonly = env.get("AZURE_DEVOPS_READONLY", "").lower() == "true"
            self._register_azure_devops(registry, readonly)

    def _register_azure_devops(self, registry: ToolRegistry, readonly: bool) -> None:
        """Register Azure DevOps handler."""
        from ..tools.handlers.azure_devops import AzureDevOpsHandler
        requires_approval = self.profile.requires_tool_approval("azure_devops")
        handler = AzureDevOpsHandler(readonly=readonly, requires_approval=requires_approval)
        registry.register(handler)


def create_registry_from_profile(
    profile: CapabilityProfile,
    working_dir: str | None = None,
) -> ToolRegistry:
    """Convenience function to create a registry from a profile.

    Args:
        profile: The capability profile.
        working_dir: Working directory for shell commands.

    Returns:
        Configured tool registry.
    """
    factory = ToolFactory(profile)
    return factory.create_registry(working_dir=working_dir)


def load_profile(name_or_path: str) -> CapabilityProfile:
    """Load a profile by name or path.

    Args:
        name_or_path: Either a built-in profile name ('readonly', 'developer', 'eval')
                     or a path to a YAML profile file.

    Returns:
        The loaded capability profile.

    Raises:
        FileNotFoundError: If the profile file doesn't exist.
        ValueError: If the profile name is unknown.
    """
    # Check for built-in profiles
    builtins = {
        "readonly": CapabilityProfile.readonly,
        "developer": CapabilityProfile.developer,
        "eval": CapabilityProfile.eval,
    }

    if name_or_path in builtins:
        return builtins[name_or_path]()

    # Check if it's a file path
    path = Path(name_or_path).expanduser()
    if path.exists():
        return CapabilityProfile.from_yaml(path)

    # Check in default profile directories
    profile_dirs = [
        Path.home() / ".config" / "ro-agent" / "profiles",
        Path(__file__).parent / "profiles",
    ]

    for profile_dir in profile_dirs:
        yaml_path = profile_dir / f"{name_or_path}.yaml"
        if yaml_path.exists():
            return CapabilityProfile.from_yaml(yaml_path)

    raise ValueError(
        f"Unknown profile: {name_or_path}. "
        f"Use 'readonly', 'developer', 'eval', or provide a path to a YAML file."
    )
