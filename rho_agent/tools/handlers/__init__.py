"""Tool handlers for rho-agent.

This module exports the standard 8-tool agentic toolkit plus database tools:

Core Tools (always available):
- ReadHandler (read) - Read file contents with line numbers
- GlobHandler (glob) - Find files by pattern using ripgrep
- GrepHandler (grep) - Search file contents using ripgrep
- ListHandler (list) - List directory contents
- ReadExcelHandler (read_excel) - Read Excel files

Mode-dependent Tools:
- BashHandler (bash) - Shell execution (restricted or unrestricted)
- WriteHandler (write) - File writing (off, create-only, or full)
- EditHandler (edit) - Surgical file editing (only with file_write=full)
- DelegateHandler (delegate) - Spawn one child agent for focused sub-work

Database Tools (enabled via env vars):
- OracleHandler (oracle)
- MysqlHandler (mysql)
- SqliteHandler (sqlite)
- VerticaHandler (vertica)
- PostgresHandler (postgres)
"""

# Core tools
from .read import ReadHandler
from .glob import GlobHandler
from .grep import GrepHandler
from .list import ListHandler
from .read_excel import ReadExcelHandler

# Mode-dependent tools
from .bash import BashHandler
from .write import WriteHandler
from .delegate import DelegateHandler
from .edit import EditHandler

# Database tools
from .mysql import MysqlHandler
from .oracle import OracleHandler
from .postgres import PostgresHandler
from .sqlite import SqliteHandler
from .vertica import VerticaHandler

__all__ = [
    # Core tools
    "ReadHandler",
    "GlobHandler",
    "GrepHandler",
    "ListHandler",
    "ReadExcelHandler",
    # Mode-dependent tools
    "BashHandler",
    "WriteHandler",
    "EditHandler",
    "DelegateHandler",
    # Database handlers
    "MysqlHandler",
    "OracleHandler",
    "PostgresHandler",
    "SqliteHandler",
    "VerticaHandler",
]
