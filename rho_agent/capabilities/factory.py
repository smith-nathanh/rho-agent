"""Tool factory for creating registries from capability profiles."""

import os
from pathlib import Path
from typing import Any

from ..config.databases import DatabaseConfig, load_database_config
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

        # Register core tools (unless bash_only mode)
        if not self.profile.bash_only:
            self._register_core_tools(registry)

        # Register bash tool (mode depends on profile)
        self._register_bash_tool(registry, working_dir)

        # Register write/edit tools (if enabled and not bash_only)
        if not self.profile.bash_only:
            self._register_write_tools(registry)

        # Register database and external service tools unless bash_only mode
        if not self.profile.bash_only:
            self._register_database_tools(registry, env)
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

        registry.register(
            WriteHandler(
                create_only=create_only,
                requires_approval=requires_write_approval,
            )
        )

        # Edit tool only available in FULL mode
        if self.profile.file_write == FileWriteMode.FULL:
            requires_edit_approval = self.profile.requires_tool_approval("edit")
            registry.register(EditHandler(requires_approval=requires_edit_approval))

    def _register_database_tools(self, registry: ToolRegistry, env: dict[str, str]) -> None:
        """Register database tools from config file."""
        readonly = self.profile.database == DatabaseMode.READONLY
        db_configs = self._load_database_configs(env)

        for db_type, configs in db_configs.items():
            self._register_db_handler(registry, db_type, configs, readonly)

    def _load_database_configs(self, env: dict[str, str]) -> dict[str, list[DatabaseConfig]]:
        """Load multi-database configuration if available.

        Raises:
            ValueError: If config file exists but is malformed.
        """
        return load_database_config(env=env)

    def _register_db_handler(
        self,
        registry: ToolRegistry,
        db_type: str,
        configs: list[DatabaseConfig],
        readonly: bool,
    ) -> None:
        """Register a database handler with the given configs."""
        requires_approval = self.profile.requires_tool_approval(db_type)

        if db_type == "sqlite":
            from ..tools.handlers.sqlite import SqliteHandler

            handler = SqliteHandler(
                configs=configs, readonly=readonly, requires_approval=requires_approval
            )
            registry.register(handler)
        elif db_type == "postgres":
            try:
                from ..tools.handlers.postgres import PostgresHandler

                handler = PostgresHandler(
                    configs=configs, readonly=readonly, requires_approval=requires_approval
                )
                registry.register(handler)
            except ImportError:
                pass
        elif db_type == "mysql":
            try:
                from ..tools.handlers.mysql import MysqlHandler

                handler = MysqlHandler(
                    configs=configs, readonly=readonly, requires_approval=requires_approval
                )
                registry.register(handler)
            except ImportError:
                pass
        elif db_type == "oracle":
            try:
                from ..tools.handlers.oracle import OracleHandler

                handler = OracleHandler(
                    configs=configs, readonly=readonly, requires_approval=requires_approval
                )
                registry.register(handler)
            except ImportError:
                pass
        elif db_type == "vertica":
            try:
                from ..tools.handlers.vertica import VerticaHandler

                handler = VerticaHandler(
                    configs=configs, readonly=readonly, requires_approval=requires_approval
                )
                registry.register(handler)
            except ImportError:
                pass

    def _register_external_services(self, registry: ToolRegistry, env: dict[str, str]) -> None:
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
        Path.home() / ".config" / "rho-agent" / "profiles",
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
