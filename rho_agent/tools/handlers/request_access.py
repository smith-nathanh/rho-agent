"""request_access: the agent asks the user for access outside its sandbox."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..base import ToolHandler, ToolInvocation, ToolOutput
from .paths import resolve_path, sandbox_for

if TYPE_CHECKING:
    from ...sandbox import Sandbox


class RequestAccessHandler(ToolHandler):
    """Ask the user to allow writing a directory, or reading a protected path,
    for the rest of the session.

    Only offered when someone can answer (the session has an approval
    callback). The answer is a grant on the calling session; every tool then
    sees it, including commands, whose sandbox is built per call.
    """

    # Session leaves this tool out when there is no approval callback.
    needs_approver = True

    def __init__(self, sandbox: Sandbox, working_dir: str | None = None) -> None:
        self._sandbox = sandbox
        self._working_dir = working_dir

    @property
    def name(self) -> str:
        return "request_access"

    @property
    def description(self) -> str:
        return (
            "Ask the user for access outside your sandbox: write access to a directory "
            "outside the writable ones, or read access to a protected path such as "
            "~/.ssh. Use it when a command fails with 'Operation not permitted' or "
            "'Read-only file system' on a path you need, or before work you know needs "
            "it. Ask for the narrowest directory that will do. If the user agrees, the "
            "access lasts for the rest of the session; if not, find another way or "
            "explain what you need."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The directory (or protected path) to get access to.",
                },
                "access": {
                    "type": "string",
                    "enum": ["write", "read"],
                    "description": "write (the default) or read.",
                },
                "reason": {
                    "type": "string",
                    "description": "One sentence, shown to the user: why you need it.",
                },
            },
            "required": ["path", "reason"],
        }

    async def handle(self, invocation: ToolInvocation) -> ToolOutput:
        args = invocation.arguments
        path_arg = str(args.get("path", "")).strip()
        write = str(args.get("access", "write")).lower() != "read"
        reason = str(args.get("reason", "")).strip()
        if not path_arg:
            return ToolOutput(content="No path given.", success=False)

        session = invocation.session
        if session is None or session.approval_callback is None:
            return ToolOutput(
                content="Nobody is available to approve access; work within the sandbox.",
                success=False,
            )

        path = resolve_path(path_arg, self._working_dir)
        if write and path.is_file():
            path = path.parent  # grant directories, not single files
        sandbox = sandbox_for(self._sandbox, invocation)
        assert sandbox is not None
        grant, why_not = sandbox.policy.plan_grant(path, write=write)
        if grant is None:
            already = why_not.endswith(("already writable.", "already readable."))
            return ToolOutput(content=why_not, success=already)

        approved = await session.ask_approval(
            "request_access",
            {"path": grant.path, "access": "write" if write else "read", "reason": reason},
        )
        if not approved:
            return ToolOutput(
                content=(
                    f"The user declined {'write' if write else 'read'} access to {grant.path}. "
                    "Do not ask again for it; find another way or explain what you need."
                ),
                success=False,
            )
        session.grant_access(grant)
        return ToolOutput(
            content=(
                f"Granted {'write' if write else 'read'} access to {grant.path} for this "
                "session. Retry what you were doing."
            ),
            metadata={"grant": grant.to_dict()},
        )
