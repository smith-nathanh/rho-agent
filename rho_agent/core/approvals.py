"""Approval queue — tool approvals answered from another process.

A session with nobody at its terminal files each approval request as a JSON
file in ``<session_dir>/approvals/`` and waits. The monitor lists pending
requests and answers one by writing a response file next to it.

    approvals/<id>.json            request, written by the session
    approvals/<id>.response.json   answer, written by the monitor

The session removes both files once it has read the answer.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .session_meta import now_iso

if TYPE_CHECKING:
    from .session import Session

APPROVALS_DIR = "approvals"
_RESPONSE_SUFFIX = ".response.json"


@dataclass
class ApprovalRequest:
    """A tool call waiting for an answer."""

    id: str
    session_id: str
    tool_name: str
    tool_args: dict[str, Any] = field(default_factory=dict)
    requested_at: str = ""
    path: Path | None = None


def _write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _response_path(request_path: Path) -> Path:
    return request_path.with_name(request_path.stem + _RESPONSE_SUFFIX)


def pending_requests(session_dir: Path) -> list[ApprovalRequest]:
    """Unanswered approval requests in a session directory, oldest first."""
    approvals_dir = session_dir / APPROVALS_DIR
    if not approvals_dir.is_dir():
        return []
    requests = []
    for path in approvals_dir.glob("*.json"):
        if path.name.endswith(_RESPONSE_SUFFIX) or _response_path(path).exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        requests.append(
            ApprovalRequest(
                id=path.stem,
                session_id=str(data.get("session_id", session_dir.name)),
                tool_name=str(data.get("tool_name", "")),
                tool_args=data.get("tool_args") or {},
                requested_at=str(data.get("requested_at", "")),
                path=path,
            )
        )
    requests.sort(key=lambda r: r.requested_at)
    return requests


def respond(request: ApprovalRequest, approved: bool) -> None:
    """Answer a pending request. The waiting session picks the answer up."""
    if request.path is None:
        raise ValueError("Request has no file to answer")
    _write_json(
        _response_path(request.path),
        {"approved": approved, "responded_at": now_iso()},
    )


class ApprovalQueue:
    """Approval callback that waits for an answer from another process.

    Use as ``session.approval_callback = ApprovalQueue(session)``. Each call
    files a request and returns once it is answered, or returns False if the
    session is cancelled while waiting. Requests and answers are recorded in
    the session trace.
    """

    def __init__(
        self,
        session: Session,
        *,
        poll_seconds: float = 0.5,
        on_request: Callable[[ApprovalRequest], None] | None = None,
    ) -> None:
        """
        Args:
            session: The session whose approvals are queued.
            poll_seconds: How often to look for an answer.
            on_request: Called with each request as it is filed, for example
                to tell the user where to answer it.
        """
        if session._session_dir is None:
            raise ValueError("ApprovalQueue needs a session with a session directory")
        self._session = session
        self._dir = session._session_dir / APPROVALS_DIR
        self._poll_seconds = poll_seconds
        self._on_request = on_request

    def for_session(self, session: Session) -> ApprovalQueue:
        """A queue for a child session, so its requests are filed under its own ID.

        A child without a session directory shares this queue.
        """
        if session._session_dir is None:
            return self
        return ApprovalQueue(session, poll_seconds=self._poll_seconds, on_request=self._on_request)

    async def __call__(self, tool_name: str, tool_args: dict[str, Any]) -> bool:
        request_id = uuid.uuid4().hex[:6]
        self._dir.mkdir(parents=True, exist_ok=True)
        request_path = self._dir / f"{request_id}.json"
        response_path = _response_path(request_path)
        request = ApprovalRequest(
            id=request_id,
            session_id=self._session.id,
            tool_name=tool_name,
            tool_args=tool_args,
            requested_at=now_iso(),
            path=request_path,
        )
        _write_json(
            request_path,
            {
                "id": request.id,
                "session_id": request.session_id,
                "tool_name": tool_name,
                "tool_args": tool_args,
                "requested_at": request.requested_at,
            },
        )
        if self._on_request is not None:
            self._on_request(request)
        state = self._session.state
        state._emit(
            {
                "type": "approval_requested",
                "request_id": request_id,
                "tool_name": tool_name,
                "tool_args": tool_args,
            }
        )

        approved = False
        outcome = "cancelled"
        try:
            while not self._session._is_cancelled():
                if response_path.exists():
                    try:
                        answer = json.loads(response_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        answer = {}
                    approved = answer.get("approved") is True
                    outcome = "approved" if approved else "denied"
                    break
                await asyncio.sleep(self._poll_seconds)
        finally:
            request_path.unlink(missing_ok=True)
            response_path.unlink(missing_ok=True)
            state._emit(
                {
                    "type": "approval_resolved",
                    "request_id": request_id,
                    "tool_name": tool_name,
                    "outcome": outcome,
                }
            )
        return approved
