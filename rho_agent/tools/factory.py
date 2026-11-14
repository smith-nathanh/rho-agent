"""Build the tool registry for an agent from its access level and tool list."""

from __future__ import annotations

import os
from collections.abc import Iterable
from typing import Any

from ..sandbox import Access, AccessLevel, Sandbox, create_sandbox
from .handlers.database_config import DatabaseConfig, _parse_database_entry
from .registry import ToolRegistry

DATABASE_TYPES = ("sqlite", "postgres", "mysql", "oracle", "vertica")
KNOWN_TOOLS = (
    "read",
    "grep",
    "glob",
    "list",
    "read_excel",
    "bash",
    "view_image",
    "write",
    "edit",
    "delegate",
    "request_access",
    *DATABASE_TYPES,
)


def build_registry(
    access: Access | str = "workspace",
    *,
    working_dir: str | None = None,
    tools: Iterable[str] | None = None,
    databases: dict[str, dict[str, Any]] | None = None,
    env: dict[str, str] | None = None,
    parallel_bash: bool = False,
) -> ToolRegistry:
    """Create the tools an agent gets.

    Args:
        access: What the agent may change. Read-only access leaves out the
            file-editing tools; read-only and workspace run commands in the
            OS sandbox and add ``request_access``.
        working_dir: Where commands run and relative paths resolve. File tools
            given None keep resolving against the process's current directory.
        tools: Tool names to keep. None keeps every tool the access level
            allows. ``request_access`` is kept whenever there is a sandbox.
        databases: Database connections (alias -> {type, ...}). None adds no
            database tools.
        env: Environment for ``${VAR}`` interpolation in database settings.
        parallel_bash: Let several bash calls in one turn run concurrently.

    Raises:
        SandboxUnavailableError: the access level needs a sandbox this machine
            cannot provide.

    The registry's ``sandbox`` attribute is the sandbox in use (None for full
    access).
    """
    access = Access.from_value(access)
    wanted = None if tools is None else set(tools)
    if wanted is not None and wanted - set(KNOWN_TOOLS):
        raise ValueError(
            f"Unknown tools: {', '.join(sorted(wanted - set(KNOWN_TOOLS)))}. "
            f"Known: {', '.join(KNOWN_TOOLS)}"
        )
    sandbox = create_sandbox(access, working_dir or os.getcwd())
    registry = ToolRegistry()
    registry.sandbox = sandbox

    def keep(name: str) -> bool:
        return wanted is None or name in wanted

    attach_images = wanted is None or "view_image" in wanted
    for handler in _core_tools(access, sandbox, working_dir, parallel_bash, attach_images):
        if keep(handler.name):
            registry.register(handler)

    if databases is not None:
        for handler in _database_tools(databases, env or dict(os.environ), access):
            if keep(handler.name):
                registry.register(handler)

    if keep("delegate"):
        from .handlers.delegate import DelegateHandler

        registry.register(DelegateHandler())

    if sandbox is not None:
        from .handlers.request_access import RequestAccessHandler

        registry.register(RequestAccessHandler(sandbox, working_dir=working_dir))

    return registry


def _core_tools(
    access: Access,
    sandbox: Sandbox | None,
    working_dir: str | None,
    parallel_bash: bool,
    attach_images: bool,
) -> list[Any]:
    """The built-in file and shell tools, in a fixed order (it is part of the prompt)."""
    from .handlers.bash import BashHandler
    from .handlers.glob import GlobHandler
    from .handlers.grep import GrepHandler
    from .handlers.list import ListHandler
    from .handlers.read import ReadHandler
    from .handlers.view_image import ViewImageHandler

    handlers: list[Any] = [
        ReadHandler(attach_images=attach_images, working_dir=working_dir, sandbox=sandbox),
        GlobHandler(working_dir=working_dir, sandbox=sandbox),
        GrepHandler(working_dir=working_dir, sandbox=sandbox),
        ListHandler(working_dir=working_dir, sandbox=sandbox),
    ]
    try:
        from .handlers.read_excel import ReadExcelHandler

        handlers.append(ReadExcelHandler(working_dir=working_dir, sandbox=sandbox))
    except ImportError:
        pass
    handlers.append(
        BashHandler(
            working_dir=working_dir or os.getcwd(),
            sandbox=sandbox,
            allow_parallel=parallel_bash,
        )
    )
    handlers.append(ViewImageHandler(working_dir=working_dir, sandbox=sandbox))
    if access.level != AccessLevel.READ_ONLY:
        from .handlers.edit import EditHandler
        from .handlers.write import WriteHandler

        handlers.append(WriteHandler(working_dir=working_dir, sandbox=sandbox))
        handlers.append(EditHandler(working_dir=working_dir, sandbox=sandbox))
    return handlers


def _database_tools(
    databases: dict[str, dict[str, Any]], env: dict[str, str], access: Access
) -> list[Any]:
    """One handler per database type. Read-only access forces read-only queries."""
    by_type: dict[str, list[DatabaseConfig]] = {}
    for alias, entry in databases.items():
        if not isinstance(entry, dict):
            raise ValueError(f"Database '{alias}' must be a dictionary")
        config = _parse_database_entry(alias, entry, env)
        by_type.setdefault(config.type, []).append(config)

    force_readonly = access.level == AccessLevel.READ_ONLY
    handlers: list[Any] = []
    for db_type, configs in by_type.items():
        handler_cls = _database_handler_class(db_type)
        if handler_cls is not None:
            handlers.append(handler_cls(configs=configs, readonly=force_readonly))
    return handlers


def _database_handler_class(db_type: str) -> Any | None:
    """The handler class for a database type, or None if its driver is missing."""
    try:
        if db_type == "sqlite":
            from .handlers.sqlite import SqliteHandler

            return SqliteHandler
        if db_type == "postgres":
            from .handlers.postgres import PostgresHandler

            return PostgresHandler
        if db_type == "mysql":
            from .handlers.mysql import MysqlHandler

            return MysqlHandler
        if db_type == "oracle":
            from .handlers.oracle import OracleHandler

            return OracleHandler
        if db_type == "vertica":
            from .handlers.vertica import VerticaHandler

            return VerticaHandler
    except ImportError:
        return None
    return None
