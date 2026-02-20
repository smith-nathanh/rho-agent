"""Capability system for configurable tool profiles."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml


class ShellMode(Enum):
    """Shell execution mode.

    RESTRICTED: Only allowlisted commands, dangerous patterns blocked
    UNRESTRICTED: Any command allowed (rely on container/sandbox for security)
    """

    RESTRICTED = "restricted"
    UNRESTRICTED = "unrestricted"


class FileWriteMode(Enum):
    """File writing mode.

    OFF: No file writing capabilities
    CREATE_ONLY: Can create new files, cannot overwrite existing
    FULL: Full write/edit capabilities (create, overwrite, edit)
    """

    OFF = "off"
    CREATE_ONLY = "create-only"
    FULL = "full"


class DatabaseMode(Enum):
    """Database access mode.

    READONLY: SELECT queries only, mutations blocked
    MUTATIONS: Full database access including INSERT/UPDATE/DELETE
    """

    READONLY = "readonly"
    MUTATIONS = "mutations"


class ApprovalMode(Enum):
    """Tool approval mode.

    ALL: All tools require approval
    DANGEROUS: Only dangerous tools require approval (shell, write, database)
    GRANULAR: Per-tool approval configuration
    NONE: No approval required (for sandboxed environments)
    """

    ALL = "all"
    DANGEROUS = "dangerous"
    GRANULAR = "granular"
    NONE = "none"


# Default tools that require approval in DANGEROUS mode
DEFAULT_DANGEROUS_TOOLS = frozenset(
    {"bash", "write", "edit", "oracle", "mysql", "sqlite", "vertica", "postgres", "delegate"}
)

# Default patterns that always require approval regardless of mode
DEFAULT_DANGEROUS_PATTERNS = (
    "rm -rf",
    "rm -r",
    "DROP TABLE",
    "DROP DATABASE",
    "TRUNCATE",
    "DELETE FROM",
    "> /dev/",
    ":(){ :|:& };:",  # fork bomb
    "mkfs",
    "dd if=",
)


@dataclass
class CapabilityProfile:
    """Configuration profile for rho-agent capabilities.

    Bundles all capability settings into a single object that can be loaded
    from YAML or constructed programmatically.
    """

    name: str
    description: str = ""

    # Capability modes
    shell: ShellMode = ShellMode.RESTRICTED
    file_write: FileWriteMode = FileWriteMode.OFF
    database: DatabaseMode = DatabaseMode.READONLY

    # Approval settings
    approval: ApprovalMode = ApprovalMode.DANGEROUS
    approval_required_tools: frozenset[str] = field(default_factory=lambda: DEFAULT_DANGEROUS_TOOLS)
    dangerous_patterns: tuple[str, ...] = DEFAULT_DANGEROUS_PATTERNS

    # Optional overrides
    shell_timeout: int = 120
    shell_working_dir: str | None = None
    bash_only: bool = False  # If True, only bash tool is registered (no Read, Grep, etc.)

    @classmethod
    def readonly(cls) -> CapabilityProfile:
        """Default read-only profile for research and inspection."""
        return cls(
            name="readonly",
            description="Read-only research profile with restricted shell",
            shell=ShellMode.RESTRICTED,
            file_write=FileWriteMode.OFF,
            database=DatabaseMode.READONLY,
            approval=ApprovalMode.DANGEROUS,
        )

    @classmethod
    def developer(cls) -> CapabilityProfile:
        """Developer profile with full file editing and unrestricted shell."""
        return cls(
            name="developer",
            description="Development profile with file editing",
            shell=ShellMode.UNRESTRICTED,
            file_write=FileWriteMode.FULL,
            database=DatabaseMode.READONLY,
            approval=ApprovalMode.GRANULAR,
            approval_required_tools=frozenset({"oracle", "mysql"}),  # DB tools still need approval
            shell_timeout=300,
        )

    @classmethod
    def eval(cls, working_dir: str = "/app") -> CapabilityProfile:
        """Evaluation profile for sandboxed containers - no restrictions."""
        return cls(
            name="eval",
            description="Evaluation profile for sandboxed environments",
            shell=ShellMode.UNRESTRICTED,
            file_write=FileWriteMode.FULL,
            database=DatabaseMode.MUTATIONS,
            approval=ApprovalMode.NONE,
            shell_timeout=300,
            shell_working_dir=working_dir,
            bash_only=False,  # Set True for bash-only mode
        )

    @classmethod
    def daytona(cls, working_dir: str = "/home/daytona") -> CapabilityProfile:
        """Remote sandbox profile - all tools execute in a Daytona cloud VM."""
        return cls(
            name="daytona",
            description="Remote sandbox - tools execute in Daytona cloud VM",
            shell=ShellMode.UNRESTRICTED,
            file_write=FileWriteMode.FULL,
            database=DatabaseMode.READONLY,
            approval=ApprovalMode.NONE,
            shell_timeout=300,
            shell_working_dir=working_dir,
        )

    @classmethod
    def from_yaml(cls, path: str | Path) -> CapabilityProfile:
        """Load a profile from a YAML file."""
        path = Path(path).expanduser().resolve()

        if not path.exists():
            raise FileNotFoundError(f"Profile file not found: {path}")

        with open(path) as f:
            data = yaml.safe_load(f)

        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CapabilityProfile:
        """Create a profile from a dictionary (parsed YAML)."""
        # Extract shell settings
        shell_config = data.get("shell", {})
        if isinstance(shell_config, dict):
            shell_mode = ShellMode(shell_config.get("mode", "restricted"))
        else:
            shell_mode = ShellMode(shell_config) if shell_config else ShellMode.RESTRICTED

        # Extract file_write settings
        # Note: YAML parses 'off' as False, so we handle that case
        file_write_config = data.get("file_write", {})
        if isinstance(file_write_config, dict):
            mode_val = file_write_config.get("mode", "off")
            # YAML parses 'off' as boolean False
            if mode_val is False:
                mode_val = "off"
            file_write_mode = FileWriteMode(mode_val)
        else:
            # YAML parses 'off' as boolean False
            if file_write_config is False:
                file_write_mode = FileWriteMode.OFF
            else:
                file_write_mode = (
                    FileWriteMode(file_write_config) if file_write_config else FileWriteMode.OFF
                )

        # Extract database settings
        db_config = data.get("database", {})
        if isinstance(db_config, dict):
            db_mode = DatabaseMode(db_config.get("mode", "readonly"))
        else:
            db_mode = DatabaseMode(db_config) if db_config else DatabaseMode.READONLY

        # Extract approval settings
        approval_config = data.get("approval", {})
        if isinstance(approval_config, dict):
            approval_mode = ApprovalMode(approval_config.get("mode", "dangerous"))
            required_tools = frozenset(
                approval_config.get("required_tools", DEFAULT_DANGEROUS_TOOLS)
            )
            patterns = tuple(approval_config.get("dangerous_patterns", DEFAULT_DANGEROUS_PATTERNS))
        else:
            approval_mode = (
                ApprovalMode(approval_config) if approval_config else ApprovalMode.DANGEROUS
            )
            required_tools = DEFAULT_DANGEROUS_TOOLS
            patterns = DEFAULT_DANGEROUS_PATTERNS

        return cls(
            name=data.get("profile", data.get("name", "custom")),
            description=data.get("description", ""),
            shell=shell_mode,
            file_write=file_write_mode,
            database=db_mode,
            approval=approval_mode,
            approval_required_tools=required_tools,
            dangerous_patterns=patterns,
            shell_timeout=data.get("shell_timeout", 120),
            shell_working_dir=data.get("shell_working_dir"),
            bash_only=bool(data.get("bash_only", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert profile to dictionary for serialization."""
        return {
            "profile": self.name,
            "description": self.description,
            "shell": {"mode": self.shell.value},
            "file_write": {"mode": self.file_write.value},
            "database": {"mode": self.database.value},
            "approval": {
                "mode": self.approval.value,
                "required_tools": list(self.approval_required_tools),
                "dangerous_patterns": list(self.dangerous_patterns),
            },
            "shell_timeout": self.shell_timeout,
            "shell_working_dir": self.shell_working_dir,
            "bash_only": self.bash_only,
        }

    def requires_tool_approval(self, tool_name: str) -> bool:
        """Check if a specific tool requires approval under this profile."""
        if self.approval == ApprovalMode.NONE:
            return False
        if self.approval == ApprovalMode.ALL:
            return True
        if self.approval == ApprovalMode.DANGEROUS:
            return tool_name in DEFAULT_DANGEROUS_TOOLS
        # GRANULAR mode
        return tool_name in self.approval_required_tools

    def is_pattern_dangerous(self, text: str) -> bool:
        """Check if text contains any dangerous patterns."""
        text_lower = text.lower()
        return any(pattern.lower() in text_lower for pattern in self.dangerous_patterns)
