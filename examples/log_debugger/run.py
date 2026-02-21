#!/usr/bin/env python3
"""Dispatch parallel log-debugging agents across multiple working directories.

Each agent runs in read-only mode, analyzes a log file in its assigned
directory, and reports a structured diagnosis.  Results are collected
and written to a single consolidated report.

Usage:
    # With real directories (one --incident per failed service):
    uv run python examples/log_debugger/run.py \
        --incident /var/log/myapp:app.log:myapp-api \
        --incident /var/log/worker:worker.log:celery-worker \
        --output report.json

    # Demo mode (creates fake log dirs under /tmp and runs against them):
    uv run python examples/log_debugger/run.py --demo --output report.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import sys
import textwrap
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from rho_agent import Agent, AgentConfig, Session


@dataclass
class Incident:
    """A single incident to investigate."""

    working_dir: str
    log_file: str
    service_name: str


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

PROMPT_PATH = Path(__file__).parent / "debug.md"


def load_system_prompt(incident: Incident) -> str:
    """Load debug.md and render template variables."""
    raw = PROMPT_PATH.read_text(encoding="utf-8")

    # Strip YAML frontmatter — the runtime uses the rendered string directly
    parts = raw.split("---", 2)
    if len(parts) >= 3:
        body = parts[2].strip()
    else:
        body = raw

    replacements = {
        "{{ platform }}": platform.system(),
        "{{ home_dir }}": str(Path.home()),
        "{{ working_dir }}": incident.working_dir,
        "{{ log_file }}": incident.log_file,
        "{{ service_name }}": incident.service_name,
    }
    for placeholder, value in replacements.items():
        body = body.replace(placeholder, value)
    return body


# ---------------------------------------------------------------------------
# User prompt sent to each agent
# ---------------------------------------------------------------------------


def build_user_prompt(incident: Incident) -> str:
    log_path = os.path.join(incident.working_dir, incident.log_file)
    return (
        f"Investigate the failure of service `{incident.service_name}`. "
        f"The primary log file is `{log_path}`. "
        f"Analyze it thoroughly and report your findings as JSON."
    )


# ---------------------------------------------------------------------------
# Result extraction
# ---------------------------------------------------------------------------


def extract_json(text: str) -> dict:
    """Extract the first JSON object from agent output."""
    decoder = json.JSONDecoder()
    for idx, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[idx:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("No JSON object found in agent output.")


# ---------------------------------------------------------------------------
# Demo scaffolding — generate fake log directories for a self-contained run
# ---------------------------------------------------------------------------

DEMO_INCIDENTS: list[tuple[str, str, str, str]] = [
    (
        "payment-service",
        "payment-service.log",
        "payment-svc",
        textwrap.dedent("""\
            2025-06-15T08:00:01Z INFO  payment-svc starting up
            2025-06-15T08:00:02Z INFO  connecting to postgres at db-prod-1.internal:5432
            2025-06-15T08:00:02Z INFO  connection pool initialized (min=5, max=20)
            2025-06-15T08:00:03Z INFO  loading merchant configurations
            2025-06-15T08:00:03Z INFO  listening on :8443
            2025-06-15T08:12:44Z WARN  slow query detected: SELECT * FROM transactions WHERE merchant_id = 4821 (1.8s)
            2025-06-15T08:30:00Z INFO  health check OK
            2025-06-15T09:14:22Z ERROR connection to postgres lost: read tcp 10.0.3.12:54210->10.0.3.50:5432: i/o timeout
            2025-06-15T09:14:22Z ERROR failed to execute query: pq: connection reset by peer
            2025-06-15T09:14:23Z WARN  retry 1/3: reconnecting to postgres
            2025-06-15T09:14:28Z WARN  retry 2/3: reconnecting to postgres
            2025-06-15T09:14:33Z WARN  retry 3/3: reconnecting to postgres
            2025-06-15T09:14:38Z FATAL all retries exhausted — cannot reach database. shutting down.
            2025-06-15T09:14:38Z INFO  graceful shutdown initiated
            2025-06-15T09:14:39Z INFO  process exited with code 1
        """),
    ),
    (
        "auth-service",
        "auth.log",
        "auth-svc",
        textwrap.dedent("""\
            2025-06-15T07:55:00Z INFO  auth-svc v3.8.1 starting
            2025-06-15T07:55:01Z INFO  loading RSA keys from /etc/auth/keys/
            2025-06-15T07:55:01Z INFO  JWT issuer configured: https://auth.example.com
            2025-06-15T07:55:02Z INFO  Redis session store connected at redis-prod:6379
            2025-06-15T07:55:02Z INFO  listening on :8080
            2025-06-15T08:00:00Z INFO  health check OK
            2025-06-15T10:22:01Z WARN  memory usage at 78% (3.1GB / 4.0GB)
            2025-06-15T10:45:12Z WARN  memory usage at 89% (3.6GB / 4.0GB)
            2025-06-15T10:45:12Z WARN  GC pressure high: 340ms pause
            2025-06-15T11:02:33Z ERROR memory usage at 97% (3.9GB / 4.0GB)
            2025-06-15T11:02:34Z ERROR GC overhead limit exceeded
            2025-06-15T11:02:34Z FATAL java.lang.OutOfMemoryError: Java heap space
                at com.example.auth.session.SessionCache.put(SessionCache.java:142)
                at com.example.auth.handler.LoginHandler.handle(LoginHandler.java:87)
                at io.netty.channel.AbstractChannelHandlerContext.invokeChannelRead(AbstractChannelHandlerContext.java:379)
            2025-06-15T11:02:35Z INFO  process killed by OOM killer (signal 9)
        """),
    ),
    (
        "notification-worker",
        "worker.log",
        "notif-worker",
        textwrap.dedent("""\
            2025-06-15T06:00:00Z INFO  notif-worker starting (PID 44821)
            2025-06-15T06:00:01Z INFO  connecting to RabbitMQ at amqp://mq-prod:5672
            2025-06-15T06:00:01Z INFO  subscribed to queue: notifications.send
            2025-06-15T06:00:02Z INFO  SMTP relay configured: smtp.internal:587
            2025-06-15T06:00:02Z INFO  ready to process messages
            2025-06-15T06:15:00Z INFO  processed 1,240 messages (0 failures)
            2025-06-15T06:30:00Z INFO  processed 2,891 messages (0 failures)
            2025-06-15T07:00:00Z INFO  processed 8,120 messages (3 transient failures, retried OK)
            2025-06-15T09:44:10Z WARN  SMTP connection refused: smtp.internal:587 — Connection refused
            2025-06-15T09:44:10Z ERROR failed to send email to user 991204: [Errno 111] Connection refused
            2025-06-15T09:44:11Z ERROR failed to send email to user 224817: [Errno 111] Connection refused
            2025-06-15T09:44:11Z WARN  SMTP circuit breaker OPEN after 5 consecutive failures
            2025-06-15T09:44:11Z ERROR message processing halted: downstream SMTP unavailable
            2025-06-15T09:50:00Z WARN  circuit breaker still OPEN, 312 messages queued
            2025-06-15T10:00:00Z WARN  circuit breaker still OPEN, 1,847 messages queued
            2025-06-15T10:05:00Z ERROR queue depth exceeded threshold (2000). Raising alert.
            2025-06-15T10:05:01Z FATAL shutting down: unable to drain queue, SMTP unreachable for 20m
        """),
    ),
    (
        "data-pipeline",
        "etl.log",
        "etl-daily",
        textwrap.dedent("""\
            2025-06-15T02:00:00Z INFO  etl-daily job started (run_id=20250615-0200)
            2025-06-15T02:00:01Z INFO  source: s3://data-lake-prod/raw/events/2025-06-14/
            2025-06-15T02:00:01Z INFO  target: warehouse.analytics.daily_events
            2025-06-15T02:00:02Z INFO  scanning source prefix... found 847 parquet files (12.4 GB)
            2025-06-15T02:00:05Z INFO  schema validation passed
            2025-06-15T02:05:00Z INFO  stage 1/3 (extract): 200/847 files processed
            2025-06-15T02:10:00Z INFO  stage 1/3 (extract): 400/847 files processed
            2025-06-15T02:15:00Z INFO  stage 1/3 (extract): 600/847 files processed
            2025-06-15T02:18:33Z ERROR stage 1/3 (extract): corrupted parquet file: s3://data-lake-prod/raw/events/2025-06-14/part-00612.parquet
            2025-06-15T02:18:33Z ERROR   ArrowInvalid: Parquet magic bytes not found in footer. Either the file is corrupted or this is not a parquet file.
            2025-06-15T02:18:33Z ERROR   file size: 0 bytes (expected ~15MB based on neighbors)
            2025-06-15T02:18:34Z WARN  skipping corrupted file, continuing extraction
            2025-06-15T02:20:00Z INFO  stage 1/3 (extract): 846/847 files processed (1 skipped)
            2025-06-15T02:20:01Z INFO  stage 2/3 (transform): starting deduplication
            2025-06-15T02:25:00Z INFO  stage 2/3 (transform): 14,221,847 rows deduplicated to 13,998,102
            2025-06-15T02:25:01Z INFO  stage 3/3 (load): inserting into warehouse
            2025-06-15T02:30:00Z ERROR stage 3/3 (load): disk full on warehouse node wh-03
            2025-06-15T02:30:00Z ERROR   OSError: [Errno 28] No space left on device
            2025-06-15T02:30:01Z ERROR   /data/warehouse/analytics/daily_events/2025-06-14/: write failed
            2025-06-15T02:30:01Z FATAL aborting ETL run 20250615-0200: load stage failed
            2025-06-15T02:30:02Z INFO  rolling back partial load
            2025-06-15T02:30:05Z INFO  rollback complete. 0 rows committed.
            2025-06-15T02:30:05Z INFO  exit code 1
        """),
    ),
    (
        "api-gateway",
        "gateway.log",
        "api-gw",
        textwrap.dedent("""\
            2025-06-15T00:00:00Z INFO  api-gw v2.1.0 starting
            2025-06-15T00:00:01Z INFO  loading route config from /etc/gateway/routes.yaml
            2025-06-15T00:00:01Z INFO  upstream backends: [payment-svc, auth-svc, user-svc, order-svc]
            2025-06-15T00:00:02Z INFO  rate limiter: 1000 req/s per client
            2025-06-15T00:00:02Z INFO  TLS: certificate loaded (expires 2025-07-20)
            2025-06-15T00:00:02Z INFO  listening on :443
            2025-06-15T06:00:00Z INFO  traffic summary: 142,891 requests, 99.7% success rate
            2025-06-15T09:14:40Z WARN  upstream payment-svc unhealthy: 3 consecutive failures
            2025-06-15T09:14:40Z WARN  circuit breaker OPEN for payment-svc
            2025-06-15T09:14:41Z ERROR 502 Bad Gateway: POST /api/v1/payments — upstream connection refused
            2025-06-15T09:14:42Z ERROR 502 Bad Gateway: POST /api/v1/payments — upstream connection refused
            2025-06-15T09:15:00Z WARN  error rate spike: 23% of requests failing (baseline: 0.3%)
            2025-06-15T09:15:01Z ERROR 502 Bad Gateway: POST /api/v1/payments — upstream connection refused
            2025-06-15T09:20:00Z WARN  error rate: 18% — payment-svc still unreachable
            2025-06-15T09:30:00Z WARN  error rate: 15% — payment-svc circuit breaker still OPEN
            2025-06-15T10:05:02Z WARN  upstream notif-worker health check failing
            2025-06-15T10:10:00Z WARN  2 of 4 upstream backends unhealthy
            2025-06-15T11:02:36Z ERROR upstream auth-svc not responding — connection timeout after 30s
            2025-06-15T11:02:36Z ERROR 504 Gateway Timeout: POST /api/v1/login
            2025-06-15T11:03:00Z FATAL 3 of 4 upstream backends down. Entering degraded mode.
            2025-06-15T11:03:00Z WARN  only order-svc reachable. Rejecting traffic to other routes.
        """),
    ),
]


def create_demo_dirs(base: Path) -> list[Incident]:
    """Write fake log files under base and return Incident list."""
    incidents = []
    for dirname, logname, svc, content in DEMO_INCIDENTS:
        d = base / dirname
        d.mkdir(parents=True, exist_ok=True)
        (d / logname).write_text(content, encoding="utf-8")
        incidents.append(Incident(working_dir=str(d), log_file=logname, service_name=svc))
    return incidents


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


async def run_dispatcher(args: argparse.Namespace) -> int:
    # Build incident list
    if args.demo:
        demo_base = Path("/tmp/rho-agent-log-debug-demo")
        if demo_base.exists():
            import shutil

            shutil.rmtree(demo_base)
        incidents = create_demo_dirs(demo_base)
        print(f"Demo mode: created {len(incidents)} fake log directories under {demo_base}")
    else:
        incidents = []
        for spec in args.incident:
            parts = spec.split(":")
            if len(parts) != 3:
                print(f"Error: --incident must be dir:logfile:service — got {spec!r}")
                return 1
            incidents.append(Incident(working_dir=parts[0], log_file=parts[1], service_name=parts[2]))

    if not incidents:
        print("No incidents to investigate. Use --incident or --demo.")
        return 1

    print(f"Dispatching {len(incidents)} debug agents in parallel...\n")

    # Create one agent+session per incident, each with its own working_dir
    sessions: list[tuple[Incident, Session]] = []

    for incident in incidents:
        system_prompt = load_system_prompt(incident)
        config = AgentConfig(
            system_prompt=system_prompt,
            model=args.model,
            profile="readonly",
            working_dir=incident.working_dir,
            auto_approve=True,
        )
        agent = Agent(config)
        session = Session(agent)
        sessions.append((incident, session))
        print(f"  [{incident.service_name}] dispatched → {incident.working_dir}/{incident.log_file}")

    print()

    # Run all agents concurrently
    async def run_one(incident: Incident, session: Session) -> tuple[Incident, object | None, Exception | None]:
        user_prompt = build_user_prompt(incident)
        try:
            result = await session.run(user_prompt)
            return incident, result, None
        except Exception as exc:
            return incident, None, exc

    tasks = [run_one(inc, sess) for inc, sess in sessions]
    outcomes = await asyncio.gather(*tasks)

    # Collect results
    reports: list[dict] = []
    errors: list[dict] = []

    for incident, result, exc in outcomes:
        if exc is not None:
            errors.append({
                "service": incident.service_name,
                "error": str(exc),
            })
            print(f"  [{incident.service_name}] failed: {exc}")
            continue

        if result and result.status == "completed" and result.text.strip():
            try:
                report = extract_json(result.text)
                reports.append(report)
                root_cause = report.get("root_cause", "unknown")
                category = report.get("category", "unknown")
                severity = report.get("severity", "unknown")
                print(f"  [{incident.service_name}] done — {severity} {category}: {root_cause}")
            except ValueError:
                errors.append({
                    "service": incident.service_name,
                    "error": "Agent did not return valid JSON",
                    "raw_output": result.text[:500],
                })
                print(f"  [{incident.service_name}] done — could not parse JSON from output")
        else:
            status = result.status if result else "unknown"
            errors.append({
                "service": incident.service_name,
                "error": f"Agent finished with status: {status}",
                "raw_output": result.text[:500] if result and result.text else "",
            })
            print(f"  [{incident.service_name}] finished with status: {status}")

    # Build consolidated report
    consolidated = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": args.model,
        "total_incidents": len(incidents),
        "diagnosed": len(reports),
        "failed": len(errors),
        "summary": build_summary(reports),
        "reports": reports,
        "errors": errors if errors else None,
    }

    # Write output
    output_path = Path(args.output)
    output_path.write_text(json.dumps(consolidated, indent=2), encoding="utf-8")

    print(f"\nConsolidated report written to: {output_path}")
    print(f"  {len(reports)} diagnosed, {len(errors)} errors")

    # Print summary table
    if reports:
        print("\n" + "=" * 72)
        print("INCIDENT SUMMARY")
        print("=" * 72)
        for r in reports:
            sev = r.get("severity", "?").upper()
            cat = r.get("category", "?")
            svc = r.get("service", "?")
            cause = r.get("root_cause", "?")
            print(f"  [{sev:>8}] {svc:<20} {cat:<22} {cause}")
        print("=" * 72)

    return 0


def build_summary(reports: list[dict]) -> dict:
    """Build a summary section from the individual reports."""
    if not reports:
        return {"note": "No incidents were successfully diagnosed."}

    by_severity: dict[str, int] = {}
    by_category: dict[str, int] = {}
    for r in reports:
        sev = r.get("severity", "unknown")
        cat = r.get("category", "unknown")
        by_severity[sev] = by_severity.get(sev, 0) + 1
        by_category[cat] = by_category.get(cat, 0) + 1

    return {
        "by_severity": by_severity,
        "by_category": by_category,
        "services_affected": [r.get("service", "unknown") for r in reports],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Dispatch parallel log-debugging agents and collect a consolidated report.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            examples:
              # Run with real incidents
              %(prog)s --incident /var/log/myapp:app.log:myapp-api \\
                       --incident /var/log/worker:worker.log:celery-worker \\
                       --output report.json

              # Demo mode with fake log files
              %(prog)s --demo --output report.json
        """),
    )
    parser.add_argument(
        "--incident",
        action="append",
        default=[],
        metavar="DIR:LOGFILE:SERVICE",
        help="Incident to investigate (working_dir:log_filename:service_name). Repeatable.",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Create fake log directories under /tmp and run against them.",
    )
    parser.add_argument(
        "--output",
        default="debug_report.json",
        help="Path for the consolidated JSON report (default: debug_report.json)",
    )
    parser.add_argument(
        "--model",
        default="gpt-5-mini",
        help="Model to use for each agent (default: gpt-5-mini)",
    )
    return parser


def main() -> int:
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args()
    return asyncio.run(run_dispatcher(args))


if __name__ == "__main__":
    raise SystemExit(main())
