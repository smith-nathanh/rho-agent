"""Unit tests for Daytona cloud sandbox handlers.

All tests use mocked AsyncSandbox to verify handler behavior without
requiring a real Daytona API key.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rho_agent.tools.base import ToolInvocation
from rho_agent.tools.handlers.daytona.manager import SandboxManager
from rho_agent.tools.handlers.daytona.bash import DaytonaBashHandler
from rho_agent.tools.handlers.daytona.read import DaytonaReadHandler
from rho_agent.tools.handlers.daytona.write import DaytonaWriteHandler
from rho_agent.tools.handlers.daytona.edit import DaytonaEditHandler
from rho_agent.tools.handlers.daytona.glob import DaytonaGlobHandler
from rho_agent.tools.handlers.daytona.grep import DaytonaGrepHandler
from rho_agent.tools.handlers.daytona.list import DaytonaListHandler


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_exec_response(result: str = "", exit_code: int = 0):
    """Create a mock ExecuteResponse."""
    resp = MagicMock()
    resp.result = result
    resp.exit_code = exit_code
    return resp


def _make_sandbox():
    """Create a mock AsyncSandbox with process and fs interfaces."""
    sandbox = AsyncMock()
    sandbox.process.exec = AsyncMock(return_value=_make_exec_response())
    sandbox.fs.download_file = AsyncMock(return_value=b"")
    sandbox.fs.upload_file = AsyncMock()
    return sandbox


@pytest.fixture
def sandbox():
    return _make_sandbox()


@pytest.fixture
def manager(sandbox):
    mgr = SandboxManager()
    mgr._sandbox = sandbox
    return mgr


def _invocation(tool_name: str, **kwargs) -> ToolInvocation:
    return ToolInvocation(call_id="test-1", tool_name=tool_name, arguments=kwargs)


# ---------------------------------------------------------------------------
# DaytonaBackend
# ---------------------------------------------------------------------------


class TestDaytonaBackend:
    def test_defaults(self):
        from rho_agent.tools.handlers.daytona.backend import DaytonaBackend

        b = DaytonaBackend()
        assert b.config is None
        assert b.image == "ubuntu:latest"
        assert b.resources is None
        assert b.auto_stop_interval == 0

    def test_config_serializes_as_daytona_string(self):
        from rho_agent.core.config import AgentConfig
        from rho_agent.tools.handlers.daytona.backend import DaytonaBackend

        cfg = AgentConfig(backend=DaytonaBackend(image="python:3.13"))
        d = cfg._to_dict()
        assert d["backend"] == "daytona"


# ---------------------------------------------------------------------------
# SandboxManager
# ---------------------------------------------------------------------------


class TestSandboxManager:
    def test_defaults(self):
        mgr = SandboxManager()
        assert mgr._image == "ubuntu:latest"
        assert mgr._working_dir == "/home/daytona"
        assert mgr._api_config is None

    def test_from_backend_defaults(self):
        from rho_agent.tools.handlers.daytona.backend import DaytonaBackend

        backend = DaytonaBackend()
        mgr = SandboxManager.from_backend(backend)
        assert mgr._image == "ubuntu:latest"
        assert mgr._auto_stop_interval == 0
        assert mgr._api_config is None

    def test_from_backend_with_config(self):
        from rho_agent.tools.handlers.daytona.backend import DaytonaBackend

        fake_config = MagicMock()
        backend = DaytonaBackend(
            config=fake_config,
            image="python:3.13",
            auto_stop_interval=60,
        )
        mgr = SandboxManager.from_backend(backend, working_dir="/work")
        assert mgr._image == "python:3.13"
        assert mgr._working_dir == "/work"
        assert mgr._auto_stop_interval == 60
        assert mgr._api_config is fake_config

    def test_from_backend_extracts_resources(self):
        from rho_agent.tools.handlers.daytona.backend import DaytonaBackend

        resources = MagicMock()
        resources.cpu = 2
        resources.memory = 4
        resources.disk = 10
        resources.gpu = None
        backend = DaytonaBackend(resources=resources)
        mgr = SandboxManager.from_backend(backend)
        assert mgr._resources == {"cpu": 2, "memory": 4, "disk": 10}

    async def test_close_when_no_sandbox(self):
        mgr = SandboxManager()
        await mgr.close()  # Should not raise


# ---------------------------------------------------------------------------
# DaytonaBashHandler
# ---------------------------------------------------------------------------


class TestDaytonaBashHandler:
    async def test_name(self, manager):
        h = DaytonaBashHandler(manager)
        assert h.name == "bash"

    async def test_execute_success(self, manager, sandbox):
        sandbox.process.exec.return_value = _make_exec_response("hello\n", 0)
        h = DaytonaBashHandler(manager)
        result = await h.handle(_invocation("bash", command="echo hello"))
        assert result.success is True
        data = json.loads(result.content)
        assert data["output"] == "hello\n"
        assert data["metadata"]["exit_code"] == 0

    async def test_execute_failure(self, manager, sandbox):
        sandbox.process.exec.return_value = _make_exec_response("not found", 127)
        h = DaytonaBashHandler(manager)
        result = await h.handle(_invocation("bash", command="badcmd"))
        assert result.success is False
        data = json.loads(result.content)
        assert data["metadata"]["exit_code"] == 127

    async def test_no_command(self, manager):
        h = DaytonaBashHandler(manager)
        result = await h.handle(_invocation("bash", command=""))
        assert result.success is False
        assert "No command" in result.content

    async def test_timeout_error(self, manager, sandbox):
        sandbox.process.exec.side_effect = Exception("DaytonaTimeoutError: timed out")
        h = DaytonaBashHandler(manager)
        result = await h.handle(_invocation("bash", command="sleep 999"))
        assert result.success is False

    async def test_custom_working_dir(self, manager, sandbox):
        sandbox.process.exec.return_value = _make_exec_response("ok", 0)
        h = DaytonaBashHandler(manager)
        await h.handle(_invocation("bash", command="ls", working_dir="/tmp"))
        sandbox.process.exec.assert_called_once_with(
            "ls",
            cwd="/tmp",
            timeout=300,
        )


# ---------------------------------------------------------------------------
# DaytonaReadHandler
# ---------------------------------------------------------------------------


class TestDaytonaReadHandler:
    async def test_name(self, manager):
        h = DaytonaReadHandler(manager)
        assert h.name == "read"

    async def test_read_file(self, manager, sandbox):
        file_content = "line one\nline two\nline three\n"
        # sed output + wc -l output
        sandbox.process.exec.return_value = _make_exec_response(
            "line one\nline two\nline three\n3", 0
        )
        h = DaytonaReadHandler(manager)
        result = await h.handle(_invocation("read", path="/home/daytona/test.txt"))
        assert result.success is True
        assert "line one" in result.content
        assert "     1  line one" in result.content

    async def test_binary_rejection(self, manager):
        h = DaytonaReadHandler(manager)
        result = await h.handle(_invocation("read", path="/data/image.png"))
        assert result.success is False
        assert "binary" in result.content.lower()

    async def test_file_not_found(self, manager, sandbox):
        sandbox.process.exec.return_value = _make_exec_response(
            "sed: can't read /nope: No such file or directory", 2
        )
        h = DaytonaReadHandler(manager)
        result = await h.handle(_invocation("read", path="/nope"))
        assert result.success is False
        assert "not found" in result.content.lower() or "No such file" in result.content

    async def test_no_path(self, manager):
        h = DaytonaReadHandler(manager)
        result = await h.handle(_invocation("read", path=""))
        assert result.success is False

    async def test_empty_file_out_of_range_matches_local_behavior(self, manager, sandbox):
        sandbox.process.exec.return_value = _make_exec_response("0\n", 0)
        h = DaytonaReadHandler(manager)
        result = await h.handle(_invocation("read", path="/home/daytona/empty.txt"))
        assert result.success is False
        assert "exceeds file length (0 lines)" in result.content


# ---------------------------------------------------------------------------
# DaytonaWriteHandler
# ---------------------------------------------------------------------------


class TestDaytonaWriteHandler:
    async def test_name(self, manager):
        h = DaytonaWriteHandler(manager)
        assert h.name == "write"

    async def test_write_file(self, manager, sandbox):
        sandbox.process.exec.return_value = _make_exec_response("", 0)
        h = DaytonaWriteHandler(manager)
        result = await h.handle(
            _invocation("write", path="/home/daytona/hello.py", content="print('hello')\n")
        )
        assert result.success is True
        assert "hello.py" in result.content
        sandbox.fs.upload_file.assert_called_once()

    async def test_no_content(self, manager):
        h = DaytonaWriteHandler(manager)
        result = await h.handle(_invocation("write", path="/tmp/test.txt", content=""))
        assert result.success is False
        assert "No content" in result.content

    async def test_write_file_quotes_parent_path(self, manager, sandbox):
        sandbox.process.exec.return_value = _make_exec_response("", 0)
        h = DaytonaWriteHandler(manager)
        await h.handle(
            _invocation(
                "write",
                path="/home/daytona/o'connor/hello.py",
                content="print('hello')\n",
            )
        )
        mkdir_cmd = sandbox.process.exec.call_args_list[0][0][0]
        assert mkdir_cmd == "mkdir -p '/home/daytona/o'\\''connor'"


# ---------------------------------------------------------------------------
# DaytonaEditHandler
# ---------------------------------------------------------------------------


class TestDaytonaEditHandler:
    async def test_name(self, manager):
        h = DaytonaEditHandler(manager)
        assert h.name == "edit"

    async def test_edit_exact_match(self, manager, sandbox):
        sandbox.fs.download_file.return_value = b"hello world\ngoodbye world\n"
        h = DaytonaEditHandler(manager)
        result = await h.handle(
            _invocation(
                "edit",
                path="/home/daytona/test.txt",
                old_string="hello world",
                new_string="hi world",
            )
        )
        assert result.success is True
        assert "exact match" in result.content
        # Check uploaded content
        uploaded = sandbox.fs.upload_file.call_args[0][0]
        assert b"hi world" in uploaded
        assert b"goodbye world" in uploaded

    async def test_edit_not_found(self, manager, sandbox):
        sandbox.fs.download_file.return_value = b"hello world\n"
        h = DaytonaEditHandler(manager)
        result = await h.handle(
            _invocation(
                "edit",
                path="/test.txt",
                old_string="nonexistent",
                new_string="replacement",
            )
        )
        assert result.success is False
        assert "not found" in result.content.lower()

    async def test_edit_no_old_string(self, manager):
        h = DaytonaEditHandler(manager)
        result = await h.handle(
            _invocation("edit", path="/test.txt", old_string="", new_string="x")
        )
        assert result.success is False


# ---------------------------------------------------------------------------
# DaytonaGlobHandler
# ---------------------------------------------------------------------------


class TestDaytonaGlobHandler:
    async def test_name(self, manager):
        h = DaytonaGlobHandler(manager)
        assert h.name == "glob"

    async def test_find_files(self, manager, sandbox):
        sandbox.process.exec.return_value = _make_exec_response(
            "/workspace/src/main.py\n/workspace/src/utils.py\n", 0
        )
        h = DaytonaGlobHandler(manager)
        result = await h.handle(_invocation("glob", pattern="*.py", path="/workspace"))
        assert result.success is True
        assert "src/main.py" in result.content
        assert "2 files found" in result.content

    async def test_no_files(self, manager, sandbox):
        sandbox.process.exec.return_value = _make_exec_response("", 0)
        h = DaytonaGlobHandler(manager)
        result = await h.handle(_invocation("glob", pattern="*.xyz", path="/workspace"))
        assert result.success is True
        assert "No files found" in result.content

    async def test_find_error_is_not_silent(self, manager, sandbox):
        sandbox.process.exec.return_value = _make_exec_response("", 1)
        h = DaytonaGlobHandler(manager)
        result = await h.handle(_invocation("glob", pattern="*.py", path="/missing"))
        assert result.success is False
        assert "Find failed" in result.content or "Directory not found" in result.content


# ---------------------------------------------------------------------------
# DaytonaGrepHandler
# ---------------------------------------------------------------------------


class TestDaytonaGrepHandler:
    async def test_name(self, manager):
        h = DaytonaGrepHandler(manager)
        assert h.name == "grep"

    async def test_search_with_rg(self, manager, sandbox):
        # First call: which rg -> success, second call: actual search
        sandbox.process.exec.side_effect = [
            _make_exec_response("/usr/bin/rg", 0),
            _make_exec_response("src/main.py:10:import os\n", 0),
        ]
        h = DaytonaGrepHandler(manager)
        result = await h.handle(_invocation("grep", pattern="import os", path="/workspace"))
        assert result.success is True
        assert "import os" in result.content
        assert "1 matches" in result.content

    async def test_search_no_matches(self, manager, sandbox):
        sandbox.process.exec.side_effect = [
            _make_exec_response("/usr/bin/rg", 0),
            _make_exec_response("", 1),
        ]
        h = DaytonaGrepHandler(manager)
        result = await h.handle(_invocation("grep", pattern="nonexistent", path="/workspace"))
        assert result.success is True
        assert "No matches" in result.content

    async def test_search_fallback_to_grep(self, manager, sandbox):
        # rg not found, falls back to grep
        sandbox.process.exec.side_effect = [
            _make_exec_response("", 1),  # which rg fails
            _make_exec_response("file.py:5:match\n", 0),
        ]
        h = DaytonaGrepHandler(manager)
        result = await h.handle(_invocation("grep", pattern="match", path="/workspace"))
        assert result.success is True
        # Verify grep was used (second call)
        call_args = sandbox.process.exec.call_args_list[1]
        assert "grep" in call_args[0][0]


# ---------------------------------------------------------------------------
# DaytonaListHandler
# ---------------------------------------------------------------------------


class TestDaytonaListHandler:
    async def test_name(self, manager):
        h = DaytonaListHandler(manager)
        assert h.name == "list"

    async def test_list_directory(self, manager, sandbox):
        sandbox.process.exec.return_value = _make_exec_response(
            "total 8\ndrwxr-xr-x 2 user user 4096 Jan  1 00:00 src/\n"
            "-rw-r--r-- 1 user user  100 Jan  1 00:00 README.md\n",
            0,
        )
        h = DaytonaListHandler(manager)
        result = await h.handle(_invocation("list", path="/workspace"))
        assert result.success is True
        assert "src/" in result.content
        assert "README.md" in result.content

    async def test_empty_directory(self, manager, sandbox):
        sandbox.process.exec.return_value = _make_exec_response("total 0\n", 0)
        h = DaytonaListHandler(manager)
        result = await h.handle(_invocation("list", path="/empty"))
        assert result.success is True

    async def test_directory_not_found(self, manager, sandbox):
        sandbox.process.exec.return_value = _make_exec_response(
            "ls: cannot access '/nope': No such file or directory", 2
        )
        h = DaytonaListHandler(manager)
        result = await h.handle(_invocation("list", path="/nope"))
        assert result.success is False


# ---------------------------------------------------------------------------
# Agent integration
# ---------------------------------------------------------------------------


class TestDaytonaBackendRouting:
    def test_agent_routes_string_daytona(self):
        """Agent with backend='daytona' should call register_daytona_tools."""
        from rho_agent.core.agent import Agent
        from rho_agent.core.config import AgentConfig

        mock_manager = MagicMock()
        with patch(
            "rho_agent.tools.handlers.daytona.register_daytona_tools",
            return_value=mock_manager,
        ) as mock_reg:
            agent = Agent(AgentConfig(backend="daytona", profile="developer"))
            mock_reg.assert_called_once()
            # String "daytona" passes backend=None
            _, kwargs = mock_reg.call_args
            assert kwargs.get("backend") is None
            assert agent._sandbox_manager is mock_manager

    def test_agent_routes_daytona_backend_object(self):
        """Agent with DaytonaBackend instance passes it through."""
        from rho_agent.core.agent import Agent
        from rho_agent.core.config import AgentConfig
        from rho_agent.tools.handlers.daytona.backend import DaytonaBackend

        backend = DaytonaBackend(image="python:3.13")
        mock_manager = MagicMock()
        with patch(
            "rho_agent.tools.handlers.daytona.register_daytona_tools",
            return_value=mock_manager,
        ) as mock_reg:
            agent = Agent(AgentConfig(backend=backend, profile="developer"))
            mock_reg.assert_called_once()
            _, kwargs = mock_reg.call_args
            assert kwargs["backend"] is backend
            assert agent._sandbox_manager is mock_manager
