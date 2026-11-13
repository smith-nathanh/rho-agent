"""Live OTLP export: a StateObserver that sends spans while a session runs.

Enabled by setting ``RHO_AGENT_OTLP_ENDPOINT``. Each session (children
included) gets an observer when it starts a run; spans are the same as the
offline ``rho-agent export --format otlp`` output, IDs included.

Environment:
    RHO_AGENT_OTLP_ENDPOINT   Base OTLP/HTTP URL (``/v1/traces`` is appended).
    RHO_AGENT_OTLP_HEADERS    ``key=value,key2=value2`` request headers.
    RHO_AGENT_OTLP_PROTOCOL   ``http/protobuf`` (default, needs the otel extra)
                              or ``http/json``.
    RHO_AGENT_OTLP_CONTENT    ``1`` to include prompts, outputs and tool I/O.

Spans are queued and sent from a background thread, so a slow or unreachable
collector never blocks the agent. Export errors are logged once and dropped.
"""

from __future__ import annotations

import atexit
import logging
import os
import queue
import threading
import time
from typing import Any

from .otlp import (
    PROTOCOLS,
    SessionSpanBuilder,
    package_version,
    parse_headers,
    protobuf_available,
    provider_name_for,
    send_spans,
    trace_id_for,
    traces_url,
)

logger = logging.getLogger(__name__)

ENDPOINT_ENV = "RHO_AGENT_OTLP_ENDPOINT"
HEADERS_ENV = "RHO_AGENT_OTLP_HEADERS"
PROTOCOL_ENV = "RHO_AGENT_OTLP_PROTOCOL"
CONTENT_ENV = "RHO_AGENT_OTLP_CONTENT"

_TRUE = {"1", "true", "yes", "on"}


class OtlpExporter:
    """Batches spans and sends them from a daemon thread."""

    def __init__(
        self,
        endpoint: str,
        *,
        headers: dict[str, str] | None = None,
        protocol: str = "http/protobuf",
        batch_size: int = 256,
        interval: float = 2.0,
        timeout: float = 10.0,
    ) -> None:
        self.endpoint = endpoint
        self.headers = headers or {}
        self.protocol = protocol
        self.batch_size = batch_size
        self.interval = interval
        self.timeout = timeout
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._version = package_version()
        self._warned = False
        self.sent = 0
        self.dropped = 0

    def submit(self, span: dict[str, Any]) -> None:
        self._queue.put(span)
        self._ensure_thread()
        if self._queue.qsize() >= self.batch_size:
            self._wake.set()

    def wake(self) -> None:
        """Send what is queued now instead of at the next interval."""
        self._wake.set()

    def flush(self, timeout: float = 5.0) -> None:
        """Wait (up to ``timeout``) until every queued span has been handled."""
        deadline = time.monotonic() + timeout
        self._wake.set()
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            self._wake.set()
            time.sleep(0.02)

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._loop, name="rho-agent-otlp", daemon=True
                )
                self._thread.start()

    def _loop(self) -> None:
        while True:
            self._wake.wait(self.interval)
            self._wake.clear()
            while True:
                batch: list[dict[str, Any]] = []
                while len(batch) < self.batch_size:
                    try:
                        batch.append(self._queue.get_nowait())
                    except queue.Empty:
                        break
                if not batch:
                    break
                try:
                    send_spans(
                        batch,
                        self.endpoint,
                        headers=self.headers,
                        protocol=self.protocol,
                        batch_size=self.batch_size,
                        timeout=self.timeout,
                        service_version=self._version,
                    )
                    self.sent += len(batch)
                except Exception as e:
                    self.dropped += len(batch)
                    if not self._warned:
                        self._warned = True
                        logger.warning("OTLP export to %s failed: %s", self.endpoint, e)
                finally:
                    for _ in batch:
                        self._queue.task_done()


class OtlpObserver:
    """StateObserver that turns a session's trace events into OTLP spans."""

    def __init__(
        self,
        session: Any,
        exporter: OtlpExporter,
        *,
        include_content: bool = False,
        provider_name: str | None = None,
    ) -> None:
        self.session = session
        self.exporter = exporter
        state = session.state
        parent = getattr(session, "parent", None)
        root = session
        while getattr(root, "parent", None) is not None:
            root = root.parent
        config = getattr(getattr(session, "agent", None), "config", None)
        self.builder = SessionSpanBuilder(
            session.id,
            exporter.submit,
            context_for_run=self._context_for_run,
            root_session_id=root.id,
            agent_name=self._agent_name(session) if parent is not None else None,
            provider_name=provider_name or provider_name_for(getattr(config, "base_url", None)),
            include_content=include_content,
            run_index=int(getattr(state, "run_count", 0) or 0),
            messages=list(getattr(state, "messages", []) or []),
        )
        self._lock = threading.Lock()

    @staticmethod
    def _agent_name(session: Any) -> str | None:
        parent_obs = _find_observer(session.parent)
        if parent_obs is not None:
            links = parent_obs.builder.child_links.get(session.id) or []
            if links:
                return links[-1].tool_name
        return None

    def _context_for_run(self, run_index: int) -> tuple[str, str | None]:
        parent = getattr(self.session, "parent", None)
        parent_obs = _find_observer(parent) if parent is not None else None
        if parent_obs is not None:
            return parent_obs.builder.context_for_child_run(self.session.id, run_index)
        return trace_id_for(self.session.id, run_index), None

    def on_event(self, event: dict[str, Any]) -> None:
        with self._lock:
            if self.builder.agent_name is None and getattr(self.session, "parent", None):
                self.builder.agent_name = self._agent_name(self.session)
            self.builder.feed(event)
        if event.get("type") == "run_end":
            self.exporter.wake()


def _find_observer(session: Any) -> OtlpObserver | None:
    state = getattr(session, "state", None)
    for obs in getattr(state, "_observers", []) or []:
        if isinstance(obs, OtlpObserver):
            return obs
    return None


_exporters: dict[tuple[Any, ...], OtlpExporter] = {}
_exporters_lock = threading.Lock()
_warned_config = False


def _flush_all() -> None:
    for exporter in list(_exporters.values()):
        exporter.flush(timeout=5.0)


def get_exporter(endpoint: str, headers: dict[str, str], protocol: str) -> OtlpExporter:
    """One shared exporter per endpoint configuration, flushed at exit."""
    key = (endpoint, tuple(sorted(headers.items())), protocol)
    with _exporters_lock:
        exporter = _exporters.get(key)
        if exporter is None:
            if not _exporters:
                atexit.register(_flush_all)
            exporter = OtlpExporter(endpoint, headers=headers, protocol=protocol)
            _exporters[key] = exporter
        return exporter


def _warn_config(message: str) -> None:
    global _warned_config
    if not _warned_config:
        _warned_config = True
        logger.warning("%s; OTLP export disabled", message)


def maybe_attach_otlp_observer(session: Any) -> OtlpObserver | None:
    """Attach an OtlpObserver to ``session`` when RHO_AGENT_OTLP_ENDPOINT is set.

    Safe to call before every run: a session gets at most one observer.
    """
    endpoint = os.getenv(ENDPOINT_ENV, "").strip()
    if not endpoint:
        return None
    existing = _find_observer(session)
    if existing is not None:
        return existing
    protocol = os.getenv(PROTOCOL_ENV, "").strip() or "http/protobuf"
    if protocol not in PROTOCOLS:
        _warn_config(f"{PROTOCOL_ENV}={protocol!r} is not one of {', '.join(PROTOCOLS)}")
        return None
    try:
        traces_url(endpoint)
    except ValueError as e:
        _warn_config(str(e))
        return None
    if protocol == "http/protobuf" and not protobuf_available():
        _warn_config(
            f"{ENDPOINT_ENV} is set but http/protobuf needs the otel extra "
            f"(pip install 'rho-agent[otel]'), or set {PROTOCOL_ENV}=http/json"
        )
        return None
    exporter = get_exporter(endpoint, parse_headers(os.getenv(HEADERS_ENV)), protocol)
    observer = OtlpObserver(
        session,
        exporter,
        include_content=os.getenv(CONTENT_ENV, "").strip().lower() in _TRUE,
    )
    session.state.add_observer(observer)
    return observer
