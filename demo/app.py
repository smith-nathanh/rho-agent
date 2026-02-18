#!/usr/bin/env python3
"""Streamlit demo app for rho-agent with SQL exploration.

Launch instructions:
    # First, seed the sample database (one-time setup)
    python demo/seed_database.py

    # Then launch the Streamlit app
    streamlit run demo/app.py

    # Or with uv
    uv run streamlit run demo/app.py

The app will open at http://localhost:8501

Features:
    - Agent Chat: Ask questions about the database in natural language
    - SQL Editor: Write and execute your own SQL queries

Environment:
    Requires OPENAI_API_KEY (or OPENAI_BASE_URL for alternative providers)
    set in .env or environment.
"""

import asyncio
import os
import sqlite3
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root
load_dotenv(Path(__file__).parent.parent / ".env")

import pandas as pd
import streamlit as st

from rho_agent.runtime import (
    RuntimeOptions,
    create_runtime,
    run_prompt,
)
from rho_agent.runtime.types import LocalRuntime

# Config
DB_PATH = Path(__file__).parent / "sample_data.db"
DB_CONFIG_PATH = Path(__file__).parent / ".demo-databases.yaml"
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
    if "pending_prompt" not in st.session_state:
        st.session_state.pending_prompt = None
    if "runtime" not in st.session_state:
        st.session_state.runtime = None


def ensure_demo_db_config() -> None:
    """Ensure runtime database config points sqlite tool at the demo DB."""
    config_yaml = f"""databases:
  demo:
    type: sqlite
    path: "{DB_PATH}"
"""
    DB_CONFIG_PATH.write_text(config_yaml, encoding="utf-8")
    os.environ["RHO_AGENT_DB_CONFIG"] = str(DB_CONFIG_PATH)


def get_runtime() -> LocalRuntime:
    """Get or create the session runtime."""
    runtime = st.session_state.runtime
    if runtime is None:
        runtime = create_runtime(
            SYSTEM_PROMPT,
            options=RuntimeOptions(
                profile="readonly",
                working_dir=str(Path(__file__).parent),
            ),
        )
        st.session_state.runtime = runtime
    return runtime


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


def render_chat_tab() -> None:
    """Render the agent chat tab."""
    # Check if we have a pending prompt to process
    pending_prompt = st.session_state.get("pending_prompt")

    # Chat messages container with scrolling
    chat_container = st.container(height=500)

    with chat_container:
        # Display message history
        for msg_idx, msg in enumerate(st.session_state.messages):
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])

                # Show tool calls if present (terminal-style formatting)
                if msg.get("tool_calls"):
                    for tc_idx, tc in enumerate(msg["tool_calls"]):
                        # Format signature like terminal: sqlite(action='describe', table='employees')
                        args = tc.get("args", {})
                        if args:
                            arg_parts = [
                                f"{k}='{v}'" if isinstance(v, str) else f"{k}={v}"
                                for k, v in args.items()
                            ]
                            sig = f"{tc['name']}({', '.join(arg_parts)})"
                        else:
                            sig = f"{tc['name']}()"

                        with st.expander(f"⚡ {sig}", expanded=False):
                            if tc.get("result"):
                                result = tc["result"]
                                # Truncate long results
                                if len(result) > 1500:
                                    result = result[:1500] + "\n... (truncated)"
                                # Use code block for monospace formatting
                                st.code(result, language=None)

                            # Add "Open in SQL Editor" button for sqlite tool calls
                            if tc["name"] == "sqlite":
                                operation = args.get("operation", "")
                                sql_to_copy = None
                                button_label = "→ Open in SQL Editor"

                                if operation == "query" and args.get("sql"):
                                    sql_to_copy = args["sql"]
                                elif operation == "describe" and args.get("table_name"):
                                    # Offer a starter SELECT query for the described table
                                    sql_to_copy = f"SELECT * FROM {args['table_name']} LIMIT 10;"
                                    button_label = "→ Open query in Editor"

                                if sql_to_copy:
                                    if st.button(button_label, key=f"copy_sql_{msg_idx}_{tc_idx}"):
                                        st.session_state.sql_editor_content = sql_to_copy
                                        st.toast("Query copied to SQL Editor!")
                                        st.rerun()

        # If processing, stream events in real-time
        if pending_prompt:
            with st.chat_message("assistant"):
                # Placeholders for streaming content
                status_placeholder = st.empty()
                text_placeholder = st.empty()
                tool_container = st.container()

                response_text = ""
                tool_calls = []
                current_tool_placeholder = None

                async def stream_events():
                    nonlocal response_text, tool_calls, current_tool_placeholder
                    runtime = get_runtime()
                    await runtime.start()

                    async def on_event(event):
                        nonlocal response_text, tool_calls, current_tool_placeholder
                        if event.type == "text" and event.content:
                            response_text += event.content
                            text_placeholder.markdown(response_text + "▌")
                        elif event.type == "tool_start":
                            status_placeholder.empty()
                            args = event.tool_args or {}
                            arg_parts = [
                                f"{k}='{v}'" if isinstance(v, str) else f"{k}={v}"
                                for k, v in args.items()
                            ]
                            sig = f"{event.tool_name}({', '.join(arg_parts)})"
                            tool_calls.append(
                                {
                                    "name": event.tool_name,
                                    "args": args,
                                    "sig": sig,
                                }
                            )
                            with tool_container:
                                current_tool_placeholder = st.empty()
                                current_tool_placeholder.markdown(f"`{sig}` ...")
                        elif event.type == "tool_end":
                            if tool_calls:
                                tool_calls[-1]["result"] = event.tool_result
                            if current_tool_placeholder:
                                sig = tool_calls[-1]["sig"] if tool_calls else event.tool_name
                                current_tool_placeholder.markdown(f"`{sig}`")
                                current_tool_placeholder = None

                    await run_prompt(runtime, pending_prompt, on_event=on_event)

                    if response_text:
                        text_placeholder.markdown(response_text)
                    status_placeholder.empty()

                # Show initial status
                status_placeholder.markdown("*Thinking...*")

                # Run the async stream
                asyncio.run(stream_events())

            # Save response to history
            st.session_state.messages.append(
                {
                    "role": "assistant",
                    "content": response_text,
                    "tool_calls": tool_calls,
                }
            )

            # Clear pending prompt and rerun to show the final response
            st.session_state.pending_prompt = None
            st.rerun()

    # Chat input at the bottom
    if prompt := st.chat_input("Ask about the database..."):
        # Add user message and set pending prompt
        st.session_state.messages.append({"role": "user", "content": prompt})
        st.session_state.pending_prompt = prompt
        st.rerun()


def render_sql_tab() -> None:
    """Render the SQL editor tab."""
    # Check if we have SQL to load into the editor
    if "sql_editor_content" in st.session_state and st.session_state.sql_editor_content:
        st.session_state.sql_editor = st.session_state.sql_editor_content
        del st.session_state.sql_editor_content

    # Initialize default if not set
    if "sql_editor" not in st.session_state:
        st.session_state.sql_editor = "SELECT * FROM employees LIMIT 10;"

    # SQL input
    sql = st.text_area(
        "Enter SQL query:",
        height=150,
        key="sql_editor",
        help="Write your own SQL queries here",
    )

    col1, col2, col3, col4 = st.columns([1, 1, 1, 2])

    with col1:
        run_clicked = st.button("Run Query", type="primary")

    with col2:
        if st.button("Clear"):
            st.session_state.sql_editor = ""
            st.rerun()

    with col3:
        if st.button("🤖 Send to Agent"):
            if sql.strip():
                # Create a message asking the agent to help with this query
                prompt = f"Help me with this SQL query:\n\n```sql\n{sql.strip()}\n```"
                st.session_state.messages.append({"role": "user", "content": prompt})
                st.session_state.pending_prompt = prompt
                st.toast("Query sent! Switch to Agent Chat tab.")
                st.rerun()
            else:
                st.toast("No SQL query to send")

    # Query history dropdown
    with col4:
        if st.session_state.query_history:
            selected = st.selectbox(
                "Query History",
                options=[""] + st.session_state.query_history[-10:],
                format_func=lambda x: x[:50] + "..."
                if len(x) > 50
                else x
                if x
                else "Select previous query...",
            )
            if selected:
                st.session_state.sql_editor = selected
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
                columns, columns=["cid", "name", "type", "notnull", "default", "pk"]
            )[["name", "type", "pk"]]
            schema_df["pk"] = schema_df["pk"].apply(lambda x: "PK" if x else "")
            st.dataframe(schema_df, width="stretch", hide_index=True)

        conn.close()


def main() -> None:
    """Main app entry point."""
    st.set_page_config(
        page_title="rho-agent SQL Demo",
        page_icon="🔍",
        layout="wide",
    )

    # Check database exists
    if not DB_PATH.exists():
        st.error(f"Database not found at {DB_PATH}")
        st.info("Run `python demo/seed_database.py` to create the sample database.")
        return

    ensure_demo_db_config()
    init_session_state()

    st.title("rho-agent SQL Demo")
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
        This demo showcases rho-agent helping users explore a SQLite database.

        **Agent Chat**: Chat with the agent to explore data
        **SQL Editor**: Write and execute your own SQL

        Try asking:
        - "What tables are available?"
        - "Show me the top 5 highest paid employees"
        - "Which department has the most active projects?"
        """)

        if st.button("Clear Conversation"):
            runtime = st.session_state.get("runtime")
            if runtime is not None:
                asyncio.run(runtime.close("completed"))
                st.session_state.runtime = None
            st.session_state.messages = []
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
