#!/usr/bin/env python3
"""Streamlit demo app for ro-agent with SQL exploration."""

import asyncio
import sqlite3
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root
load_dotenv(Path(__file__).parent.parent / ".env")

import pandas as pd
import streamlit as st

from ro_agent.core.agent import Agent, AgentEvent
from ro_agent.core.session import Session
from ro_agent.client.model import ModelClient
from ro_agent.tools.registry import ToolRegistry
from ro_agent.tools.handlers.sqlite import SqliteHandler

# Config
DB_PATH = Path(__file__).parent / "sample_data.db"
SYSTEM_PROMPT = """\
You are a helpful database assistant. You help users explore and understand the SQLite database.

Available actions:
- list_tables: Show all tables in the database
- describe: Show the schema of a specific table
- query: Execute a SELECT query to explore data

When asked about the database, first explore its structure, then help answer questions with appropriate queries.
Be concise in your explanations. Format query results clearly.
"""


def init_session_state() -> None:
    """Initialize Streamlit session state."""
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "query_history" not in st.session_state:
        st.session_state.query_history = []
    if "last_agent_query" not in st.session_state:
        st.session_state.last_agent_query = ""


def get_db_connection() -> sqlite3.Connection:
    """Get a database connection for direct queries."""
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def execute_user_query(sql: str) -> tuple[pd.DataFrame | None, str | None]:
    """Execute a user's SQL query and return results or error."""
    try:
        conn = get_db_connection()
        df = pd.read_sql_query(sql, conn)
        conn.close()
        return df, None
    except Exception as e:
        return None, str(e)


async def run_agent_turn(user_input: str) -> list[AgentEvent]:
    """Run one agent turn and collect events."""
    # Create fresh session for each conversation (or restore from state)
    session = Session(system_prompt=SYSTEM_PROMPT)

    # Restore conversation history
    for msg in st.session_state.messages:
        if msg["role"] == "user":
            session.add_user_message(msg["content"])
        elif msg["role"] == "assistant":
            session.add_assistant_message(msg["content"])

    # Set up registry with SQLite handler
    registry = ToolRegistry()
    handler = SqliteHandler(db_path=str(DB_PATH), readonly=True)
    registry.register(handler)

    # Create agent
    client = ModelClient()
    agent = Agent(session=session, registry=registry, client=client)

    # Collect events
    events: list[AgentEvent] = []
    async for event in agent.run_turn(user_input):
        events.append(event)

    return events


def render_chat_tab() -> None:
    """Render the agent chat tab."""
    # Chat messages container with scrolling
    chat_container = st.container(height=500)

    with chat_container:
        # Display message history
        for msg in st.session_state.messages:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])

                # Show tool calls if present
                if msg.get("tool_calls"):
                    for tc in msg["tool_calls"]:
                        with st.expander(f"Tool: {tc['name']}", expanded=False):
                            st.code(tc.get("query", str(tc.get("args", {}))), language="sql")
                            if tc.get("result"):
                                st.text(tc["result"][:500] + "..." if len(tc.get("result", "")) > 500 else tc.get("result", ""))

    # Copy query button (above input)
    if st.session_state.last_agent_query:
        if st.button("Copy last query to SQL Editor"):
            st.session_state.sql_editor_content = st.session_state.last_agent_query
            st.rerun()

    # Chat input at the bottom
    if prompt := st.chat_input("Ask about the database..."):
        # Add user message
        st.session_state.messages.append({"role": "user", "content": prompt})

        # Run agent and process response
        with st.spinner("Thinking..."):
            events = asyncio.run(run_agent_turn(prompt))

        # Process events
        response_text = ""
        tool_calls = []

        for event in events:
            if event.type == "text" and event.content:
                response_text += event.content
            elif event.type == "tool_start":
                tool_calls.append({
                    "name": event.tool_name,
                    "args": event.tool_args,
                    "query": event.tool_args.get("sql", "") if event.tool_args else "",
                })
            elif event.type == "tool_end":
                if tool_calls:
                    tool_calls[-1]["result"] = event.tool_result

                # Capture SQL queries for the editor
                if event.tool_name == "sqlite" and tool_calls:
                    args = tool_calls[-1].get("args", {})
                    if args.get("action") == "query" and args.get("sql"):
                        st.session_state.last_agent_query = args["sql"]

        # Save to history
        st.session_state.messages.append({
            "role": "assistant",
            "content": response_text,
            "tool_calls": tool_calls,
        })

        # Rerun to show the new messages in the container
        st.rerun()


def render_sql_tab() -> None:
    """Render the SQL editor tab."""
    # Initialize editor content from session state
    default_sql = st.session_state.get("sql_editor_content", "SELECT * FROM employees LIMIT 10;")

    # SQL input
    sql = st.text_area(
        "Enter SQL query:",
        value=default_sql,
        height=150,
        key="sql_editor",
        help="Write your own SQL queries here",
    )

    col1, col2, col3 = st.columns([1, 1, 2])

    with col1:
        run_clicked = st.button("Run Query", type="primary")

    with col2:
        if st.button("Clear"):
            st.session_state.sql_editor_content = ""
            st.rerun()

    # Query history dropdown
    with col3:
        if st.session_state.query_history:
            selected = st.selectbox(
                "Query History",
                options=[""] + st.session_state.query_history[-10:],
                format_func=lambda x: x[:50] + "..." if len(x) > 50 else x if x else "Select previous query...",
            )
            if selected:
                st.session_state.sql_editor_content = selected
                st.rerun()

    # Execute query
    if run_clicked and sql.strip():
        # Add to history if unique
        if sql.strip() not in st.session_state.query_history:
            st.session_state.query_history.append(sql.strip())

        df, error = execute_user_query(sql)

        if error:
            st.error(f"Query Error: {error}")
        elif df is not None:
            st.success(f"Returned {len(df)} rows")
            st.dataframe(df, width="stretch")

            # Export button
            csv = df.to_csv(index=False)
            st.download_button(
                label="Download CSV",
                data=csv,
                file_name="query_results.csv",
                mime="text/csv",
            )

    # Schema browser
    with st.expander("Database Schema", expanded=False):
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
        """)
        tables = [row[0] for row in cursor.fetchall()]

        for table in tables:
            st.markdown(f"**{table}**")
            cursor.execute(f"PRAGMA table_info({table})")
            columns = cursor.fetchall()
            schema_df = pd.DataFrame(
                columns,
                columns=["cid", "name", "type", "notnull", "default", "pk"]
            )[["name", "type", "pk"]]
            schema_df["pk"] = schema_df["pk"].apply(lambda x: "PK" if x else "")
            st.dataframe(schema_df, width="stretch", hide_index=True)

        conn.close()


def main() -> None:
    """Main app entry point."""
    st.set_page_config(
        page_title="ro-agent SQL Demo",
        page_icon="🔍",
        layout="wide",
    )

    # Check database exists
    if not DB_PATH.exists():
        st.error(f"Database not found at {DB_PATH}")
        st.info("Run `python demo/seed_database.py` to create the sample database.")
        return

    init_session_state()

    st.title("ro-agent SQL Demo")
    st.markdown("Explore a sample database using the AI agent or write your own SQL queries.")

    # Tabs instead of columns for better chat UX
    chat_tab, sql_tab = st.tabs(["Agent Chat", "SQL Editor"])

    with chat_tab:
        render_chat_tab()

    with sql_tab:
        render_sql_tab()

    # Sidebar with info
    with st.sidebar:
        st.markdown("### About")
        st.markdown("""
        This demo showcases ro-agent helping users explore a SQLite database.

        **Agent Chat**: Chat with the agent to explore data
        **SQL Editor**: Write and execute your own SQL

        Try asking:
        - "What tables are available?"
        - "Show me the top 5 highest paid employees"
        - "Which department has the most active projects?"
        """)

        if st.button("Clear Conversation"):
            st.session_state.messages = []
            st.session_state.last_agent_query = ""
            st.rerun()

        st.markdown("---")
        st.markdown(f"**Database**: `{DB_PATH.name}`")

        # Quick stats
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM employees")
        emp_count = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM timesheets")
        ts_count = cursor.fetchone()[0]
        conn.close()

        st.markdown(f"**Employees**: {emp_count}")
        st.markdown(f"**Timesheet entries**: {ts_count}")


if __name__ == "__main__":
    main()
