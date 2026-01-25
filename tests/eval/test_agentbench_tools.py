"""Tests for AgentBench evaluation tools.

These tests verify that the eval-specific tool handlers work correctly
and maintain compatibility with the AgentBench interface.
"""

import asyncio
import tempfile
from pathlib import Path

import pytest

from ro_agent.tools.base import ToolInvocation
from ro_agent.eval.agentbench.tools import (
    EvalSqliteHandler,
    SubmitAnswerHandler,
    FinishActionHandler,
    create_dbbench_registry,
    create_os_registry,
)


class TestEvalSqliteHandler:
    """Tests for EvalSqliteHandler."""

    @pytest.fixture
    def db_path(self, tmp_path: Path) -> Path:
        """Create a temporary SQLite database for testing."""
        import sqlite3

        db_file = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_file))
        cursor = conn.cursor()

        # Create test table
        cursor.execute("""
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT
            )
        """)

        # Insert test data
        cursor.execute("INSERT INTO users (name, email) VALUES ('Alice', 'alice@example.com')")
        cursor.execute("INSERT INTO users (name, email) VALUES ('Bob', 'bob@example.com')")
        conn.commit()
        conn.close()

        return db_file

    def test_name(self, db_path: Path) -> None:
        """Test that tool name is 'execute_sql' for AgentBench compatibility."""
        handler = EvalSqliteHandler(db_path=db_path)
        assert handler.name == "execute_sql"
        handler.close()

    def test_requires_no_approval(self, db_path: Path) -> None:
        """Test that eval handler does not require approval."""
        handler = EvalSqliteHandler(db_path=db_path)
        assert handler.requires_approval is False
        handler.close()

    @pytest.mark.asyncio
    async def test_select_query(self, db_path: Path) -> None:
        """Test SELECT query execution."""
        handler = EvalSqliteHandler(db_path=db_path)

        invocation = ToolInvocation(
            call_id="1",
            tool_name="execute_sql",
            arguments={"sql": "SELECT name, email FROM users ORDER BY name"},
        )

        result = await handler.handle(invocation)
        handler.close()

        assert result.success is True
        assert "Alice" in result.content
        assert "Bob" in result.content
        assert result.metadata["row_count"] == 2

    @pytest.mark.asyncio
    async def test_insert_query(self, db_path: Path) -> None:
        """Test INSERT query execution (mutations allowed)."""
        handler = EvalSqliteHandler(db_path=db_path)

        invocation = ToolInvocation(
            call_id="1",
            tool_name="execute_sql",
            arguments={"sql": "INSERT INTO users (name, email) VALUES ('Charlie', 'charlie@example.com')"},
        )

        result = await handler.handle(invocation)
        assert result.success is True
        assert result.metadata["rows_affected"] == 1

        # Verify insertion
        select_invocation = ToolInvocation(
            call_id="2",
            tool_name="execute_sql",
            arguments={"sql": "SELECT COUNT(*) FROM users"},
        )
        select_result = await handler.handle(select_invocation)
        handler.close()

        assert "3" in select_result.content

    @pytest.mark.asyncio
    async def test_empty_sql(self, db_path: Path) -> None:
        """Test error handling for empty SQL."""
        handler = EvalSqliteHandler(db_path=db_path)

        invocation = ToolInvocation(
            call_id="1",
            tool_name="execute_sql",
            arguments={"sql": ""},
        )

        result = await handler.handle(invocation)
        handler.close()

        assert result.success is False
        assert "No SQL query provided" in result.content

    @pytest.mark.asyncio
    async def test_invalid_sql(self, db_path: Path) -> None:
        """Test error handling for invalid SQL."""
        handler = EvalSqliteHandler(db_path=db_path)

        invocation = ToolInvocation(
            call_id="1",
            tool_name="execute_sql",
            arguments={"sql": "SELECT * FROM nonexistent_table"},
        )

        result = await handler.handle(invocation)
        handler.close()

        assert result.success is False
        assert "SQL error" in result.content


class TestSubmitAnswerHandler:
    """Tests for SubmitAnswerHandler."""

    def test_name_customizable(self) -> None:
        """Test that tool name is customizable."""
        handler1 = SubmitAnswerHandler(tool_name="commit_final_answer")
        handler2 = SubmitAnswerHandler(tool_name="answer_action")

        assert handler1.name == "commit_final_answer"
        assert handler2.name == "answer_action"

    @pytest.mark.asyncio
    async def test_answer_capture(self) -> None:
        """Test that answer is captured via callback."""
        captured_answer = None

        def on_answer(answer: str) -> None:
            nonlocal captured_answer
            captured_answer = answer

        handler = SubmitAnswerHandler(
            tool_name="commit_final_answer",
            on_answer=on_answer,
        )

        assert handler.is_submitted is False

        invocation = ToolInvocation(
            call_id="1",
            tool_name="commit_final_answer",
            arguments={"answer": "42"},
        )

        result = await handler.handle(invocation)

        assert result.success is True
        assert handler.is_submitted is True
        assert handler.submitted_answer == "42"
        assert captured_answer == "42"

    @pytest.mark.asyncio
    async def test_reset(self) -> None:
        """Test handler reset functionality."""
        handler = SubmitAnswerHandler(tool_name="answer_action")

        invocation = ToolInvocation(
            call_id="1",
            tool_name="answer_action",
            arguments={"answer": "first"},
        )
        await handler.handle(invocation)

        assert handler.is_submitted is True
        handler.reset()
        assert handler.is_submitted is False
        assert handler.submitted_answer is None


class TestFinishActionHandler:
    """Tests for FinishActionHandler."""

    def test_name(self) -> None:
        """Test that tool name is 'finish_action'."""
        handler = FinishActionHandler()
        assert handler.name == "finish_action"

    @pytest.mark.asyncio
    async def test_finish_callback(self) -> None:
        """Test that finish callback is called."""
        finished = False

        def on_finish() -> None:
            nonlocal finished
            finished = True

        handler = FinishActionHandler(on_finish=on_finish)

        assert handler.is_finished is False

        invocation = ToolInvocation(
            call_id="1",
            tool_name="finish_action",
            arguments={},
        )

        result = await handler.handle(invocation)

        assert result.success is True
        assert handler.is_finished is True
        assert finished is True


class TestFactoryFunctions:
    """Tests for registry factory functions."""

    @pytest.fixture
    def db_path(self, tmp_path: Path) -> Path:
        """Create a temporary SQLite database for testing."""
        import sqlite3

        db_file = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_file))
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE test (id INTEGER)")
        conn.commit()
        conn.close()
        return db_file

    def test_create_dbbench_registry(self, db_path: Path) -> None:
        """Test DBBench registry creation."""
        db_handler = EvalSqliteHandler(db_path=db_path)

        captured = None
        def on_answer(answer: str) -> None:
            nonlocal captured
            captured = answer

        registry, submit_handler = create_dbbench_registry(
            db_handler=db_handler,
            on_answer=on_answer,
        )

        # Check registry has expected tools
        assert registry.get("execute_sql") is not None
        assert registry.get("commit_final_answer") is not None

        # Check submit handler is returned
        assert submit_handler is not None
        assert submit_handler.is_submitted is False

        db_handler.close()

    def test_create_os_registry(self) -> None:
        """Test OS registry creation (without Docker - just structure test)."""
        # We can't easily test DockerShellHandler without Docker,
        # but we can test the registry structure
        pass  # Skip actual Docker test - would require Docker to be running


class TestBackwardsCompatibility:
    """Tests for backwards compatibility aliases."""

    def test_unrestricted_sqlite_alias(self, tmp_path: Path) -> None:
        """Test that UnrestrictedSqliteHandler is an alias for EvalSqliteHandler."""
        from ro_agent.eval.agentbench.tools import UnrestrictedSqliteHandler

        import sqlite3
        db_file = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_file))
        conn.execute("CREATE TABLE t (x INT)")
        conn.close()

        handler = UnrestrictedSqliteHandler(db_path=db_file)
        assert handler.name == "execute_sql"
        handler.close()

    def test_unrestricted_mysql_alias(self) -> None:
        """Test that UnrestrictedMySQLHandler is an alias for EvalMySQLHandler."""
        from ro_agent.eval.agentbench.tools import UnrestrictedMySQLHandler, EvalMySQLHandler

        assert UnrestrictedMySQLHandler is EvalMySQLHandler
