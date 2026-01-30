"""Streamlit dashboard for rho-agent observability."""

import os
from datetime import datetime, timedelta
from pathlib import Path

import streamlit as st

from rho_agent.observability.config import DEFAULT_TELEMETRY_DB
from rho_agent.observability.storage.sqlite import TelemetryStorage, SessionSummary, SessionDetail


def get_storage() -> TelemetryStorage:
    """Get storage instance, using environment variable or default path."""
    db_path = os.getenv("RHO_AGENT_TELEMETRY_DB", str(DEFAULT_TELEMETRY_DB))
    return TelemetryStorage(db_path)


def format_tokens(tokens: int) -> str:
    """Format token count for display."""
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.1f}M"
    if tokens >= 1_000:
        return f"{tokens / 1_000:.1f}K"
    return str(tokens)


def format_duration(start: datetime, end: datetime | None) -> str:
    """Format session duration."""
    if not end:
        return "In progress"
    delta = end - start
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    remaining_seconds = seconds % 60
    if minutes < 60:
        return f"{minutes}m {remaining_seconds}s"
    hours = minutes // 60
    remaining_minutes = minutes % 60
    return f"{hours}h {remaining_minutes}m"


def status_color(status: str) -> str:
    """Get color for status badge."""
    return {
        "active": "green",
        "completed": "blue",
        "error": "red",
    }.get(status, "gray")


def render_session_list(sessions: list[SessionSummary]) -> None:
    """Render the session list view."""
    if not sessions:
        st.info("No sessions found matching the filters.")
        return

    for session in sessions:
        with st.container():
            col1, col2, col3, col4 = st.columns([3, 2, 2, 1])

            with col1:
                st.markdown(f"**{session.session_id[:8]}...**")
                st.caption(f"{session.team_id} / {session.project_id}")

            with col2:
                st.markdown(f"`{session.model}`")
                st.caption(session.started_at.strftime("%Y-%m-%d %H:%M"))

            with col3:
                total_tokens = session.total_input_tokens + session.total_output_tokens
                st.metric("Tokens", format_tokens(total_tokens))

            with col4:
                status_badge = f":{status_color(session.status)}[{session.status}]"
                st.markdown(status_badge)

            # Click to view details
            if st.button("View Details", key=f"view_{session.session_id}"):
                st.session_state.selected_session = session.session_id

            st.divider()


def render_session_detail(detail: SessionDetail) -> None:
    """Render detailed session view."""
    # Back button
    if st.button("← Back to Sessions"):
        st.session_state.selected_session = None
        st.rerun()

    st.title(f"Session {detail.session_id[:8]}...")

    # Session metadata
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Status", detail.status)
    with col2:
        st.metric("Model", detail.model)
    with col3:
        st.metric("Total Tokens", format_tokens(detail.total_input_tokens + detail.total_output_tokens))
    with col4:
        st.metric("Tool Calls", detail.total_tool_calls)

    st.markdown("---")

    # Session info
    with st.expander("Session Information", expanded=False):
        st.json({
            "session_id": detail.session_id,
            "team_id": detail.team_id,
            "project_id": detail.project_id,
            "environment": detail.environment,
            "profile": detail.profile,
            "started_at": detail.started_at.isoformat(),
            "ended_at": detail.ended_at.isoformat() if detail.ended_at else None,
            "duration": format_duration(detail.started_at, detail.ended_at),
        })

    # Turn timeline
    st.subheader("Turn Timeline")

    for turn in detail.turns:
        with st.container():
            turn_header = f"**Turn {turn['turn_index']}**"
            if turn['user_input']:
                preview = turn['user_input'][:100] + "..." if len(turn['user_input']) > 100 else turn['user_input']
                turn_header += f" - {preview}"

            st.markdown(turn_header)

            # Turn metrics
            tcol1, tcol2, tcol3 = st.columns(3)
            with tcol1:
                st.caption(f"Input: {format_tokens(turn['input_tokens'])}")
            with tcol2:
                st.caption(f"Output: {format_tokens(turn['output_tokens'])}")
            with tcol3:
                st.caption(f"Tools: {len(turn['tool_executions'])}")

            # Tool executions
            if turn['tool_executions']:
                with st.expander(f"Tool Executions ({len(turn['tool_executions'])})", expanded=False):
                    for tool in turn['tool_executions']:
                        success_icon = "✅" if tool['success'] else "❌"
                        st.markdown(f"{success_icon} **{tool['tool_name']}** ({tool['duration_ms']}ms)")

                        if tool['arguments']:
                            st.code(str(tool['arguments']), language="json")

                        if tool['error']:
                            st.error(tool['error'])

            st.divider()


def render_analytics(storage: TelemetryStorage, team_id: str | None, project_id: str | None) -> None:
    """Render analytics view."""
    st.subheader("Token Usage by Project")

    cost_summary = storage.get_cost_summary(team_id=team_id, project_id=project_id, days=30)

    if not cost_summary:
        st.info("No data available for the selected period.")
        return

    # Token usage chart
    import pandas as pd

    df = pd.DataFrame([
        {
            "Team/Project": f"{c.team_id}/{c.project_id}",
            "Input Tokens": c.total_input_tokens,
            "Output Tokens": c.total_output_tokens,
            "Sessions": c.total_sessions,
        }
        for c in cost_summary
    ])

    st.bar_chart(df.set_index("Team/Project")[["Input Tokens", "Output Tokens"]])

    # Summary table
    st.dataframe(df, use_container_width=True)

    # Tool usage
    st.subheader("Tool Usage Statistics")

    tool_stats = storage.get_tool_stats(team_id=team_id, project_id=project_id, days=30)

    if tool_stats:
        tool_df = pd.DataFrame([
            {
                "Tool": t.tool_name,
                "Calls": t.total_calls,
                "Success Rate": f"{(t.success_count / t.total_calls * 100):.1f}%" if t.total_calls > 0 else "N/A",
                "Avg Duration": f"{t.avg_duration_ms:.0f}ms",
            }
            for t in tool_stats
        ])
        st.dataframe(tool_df, use_container_width=True)
    else:
        st.info("No tool usage data available.")


def main() -> None:
    """Main dashboard entry point."""
    st.set_page_config(
        page_title="rho-agent Observability",
        page_icon="📊",
        layout="wide",
    )

    st.title("rho-agent Observability Dashboard")

    # Initialize session state
    if "selected_session" not in st.session_state:
        st.session_state.selected_session = None

    # Get storage
    try:
        storage = get_storage()
    except Exception as e:
        st.error(f"Failed to connect to telemetry database: {e}")
        st.info(f"Expected database at: {DEFAULT_TELEMETRY_DB}")
        return

    # Sidebar filters
    with st.sidebar:
        st.header("Filters")

        # Team/Project filters
        team_id = st.text_input("Team ID", value="")
        project_id = st.text_input("Project ID", value="")

        # Status filter
        status = st.selectbox(
            "Status",
            options=["All", "active", "completed", "error"],
        )
        status_filter = None if status == "All" else status

        st.divider()

        # View selector
        view = st.radio("View", ["Sessions", "Analytics"])

    # Main content
    if st.session_state.selected_session:
        # Show session detail
        detail = storage.get_session_detail(st.session_state.selected_session)
        if detail:
            render_session_detail(detail)
        else:
            st.error("Session not found")
            st.session_state.selected_session = None
            st.rerun()
    elif view == "Sessions":
        # Show session list
        sessions = storage.list_sessions(
            team_id=team_id or None,
            project_id=project_id or None,
            status=status_filter,
            limit=50,
        )
        render_session_list(sessions)
    else:
        # Show analytics
        render_analytics(
            storage,
            team_id=team_id or None,
            project_id=project_id or None,
        )


if __name__ == "__main__":
    main()
