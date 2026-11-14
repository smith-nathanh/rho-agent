"""Tests for AGENTS.md instructions, SKILL.md skills and image input."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from rho_agent.client.model import StreamEvent, ToolCall
from rho_agent.core import project_context
from rho_agent.core.agent import Agent
from rho_agent.core.config import AgentConfig
from rho_agent.core.events import IMAGE_ATTACHMENT_PREFIX, IMAGE_TOKEN_ESTIMATE
from rho_agent.core.session import Session
from rho_agent.core.state import State
from rho_agent.export.atif import trace_to_atif
from rho_agent.tools.base import ToolHandler, ToolInvocation, ToolOutput
from rho_agent.tools.handlers import view_image
from rho_agent.tools.handlers.read import ReadHandler
from rho_agent.tools.handlers.view_image import ViewImageHandler
from rho_agent.tools.registry import ToolRegistry

# A valid 1x1 PNG.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty home directory, so the developer's own files are not picked up."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "pkg" / "sub").mkdir(parents=True)
    return root


def _skill(folder: Path, name: str, description: str, body: str = "Do the thing.") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "SKILL.md"
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n")
    return path


# ---------------------------------------------------------------------------
# AGENTS.md / CLAUDE.md
# ---------------------------------------------------------------------------


def test_instructions_are_collected_from_root_to_working_dir(home: Path, repo: Path) -> None:
    (home / ".config" / "rho-agent").mkdir(parents=True)
    (home / ".config" / "rho-agent" / "AGENTS.md").write_text("personal rule")
    (repo / "AGENTS.md").write_text("root rule")
    (repo / "pkg" / "sub" / "AGENTS.md").write_text("sub rule")
    (repo.parent / "AGENTS.md").write_text("outside the repository")

    found = project_context.discover_instructions(repo / "pkg" / "sub")

    assert [f.text for f in found] == ["personal rule", "root rule", "sub rule"]


def test_claude_md_is_used_only_where_there_is_no_agents_md(home: Path, repo: Path) -> None:
    (repo / "AGENTS.md").write_text("agents at root")
    (repo / "CLAUDE.md").write_text("claude at root")
    (repo / "pkg" / "CLAUDE.md").write_text("claude in pkg")

    found = project_context.discover_instructions(repo / "pkg")

    assert [f.text for f in found] == ["agents at root", "claude in pkg"]


def test_without_a_repository_only_the_working_dir_is_read(home: Path, tmp_path: Path) -> None:
    work = tmp_path / "loose" / "inner"
    work.mkdir(parents=True)
    (work.parent / "AGENTS.md").write_text("parent")
    (work / "AGENTS.md").write_text("here")

    assert [f.text for f in project_context.discover_instructions(work)] == ["here"]


def test_oversized_instructions_keep_the_most_specific_file(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(project_context, "MAX_INSTRUCTION_BYTES", 100)
    (repo / "AGENTS.md").write_text("r" * 500)
    (repo / "pkg" / "AGENTS.md").write_text("specific")

    found = project_context.discover_instructions(repo / "pkg")

    assert found[-1].text == "specific"
    assert found[0].text.endswith("[... truncated ...]") and len(found[0].text) < 200


def test_agent_prompt_includes_instructions_and_can_turn_them_off(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / "AGENTS.md").write_text("Always run the linter.")
    monkeypatch.setenv("RHO_AGENT_PROJECT_INSTRUCTIONS", "1")

    on = Agent(AgentConfig(system_prompt="base", model="m", working_dir=str(repo)))
    off = Agent(
        AgentConfig(
            system_prompt="base", model="m", working_dir=str(repo), project_instructions=False
        )
    )

    assert on.system_prompt.startswith("base\n\n# Project instructions")
    assert "Always run the linter." in on.system_prompt
    assert off.system_prompt == "base"


def test_remote_backends_do_not_read_local_instruction_files(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / "AGENTS.md").write_text("local only")
    monkeypatch.setenv("RHO_AGENT_PROJECT_INSTRUCTIONS", "1")
    agent = Agent.__new__(Agent)
    agent._config = AgentConfig(
        system_prompt="base", model="m", working_dir=str(repo), backend="daytona"
    )

    assert agent._project_context() == ""


# ---------------------------------------------------------------------------
# SKILL.md
# ---------------------------------------------------------------------------


def test_skills_are_found_in_project_and_personal_folders(home: Path, repo: Path) -> None:
    _skill(repo / ".agents" / "skills" / "deploy", "deploy", "Ship a release")
    _skill(repo / ".claude" / "skills" / "review", "review", "Review a diff")
    _skill(home / ".config" / "rho-agent" / "skills" / "notes", "notes", "Take notes")

    skills = {s.name: s for s in project_context.discover_skills(repo / "pkg")}

    assert set(skills) == {"deploy", "review", "notes"}
    assert skills["deploy"].path == repo / ".agents" / "skills" / "deploy" / "SKILL.md"


def test_a_project_skill_hides_a_personal_one_with_the_same_name(home: Path, repo: Path) -> None:
    _skill(home / ".claude" / "skills" / "deploy", "deploy", "personal version")
    _skill(repo / ".agents" / "skills" / "deploy", "deploy", "project version")

    (skill,) = project_context.discover_skills(repo)

    assert skill.description == "project version"


def test_a_linked_skill_folder_is_listed_once(home: Path, repo: Path) -> None:
    _skill(repo / ".agents" / "skills" / "deploy", "deploy", "Ship a release")
    (repo / ".claude").mkdir()
    (repo / ".claude" / "skills").symlink_to(repo / ".agents" / "skills")

    assert len(project_context.discover_skills(repo)) == 1


def test_malformed_skills_are_skipped(home: Path, repo: Path) -> None:
    base = repo / ".agents" / "skills"
    (base / "no-front-matter").mkdir(parents=True)
    (base / "no-front-matter" / "SKILL.md").write_text("just text")
    (base / "bad-yaml").mkdir()
    (base / "bad-yaml" / "SKILL.md").write_text("---\nname: [unclosed\n---\nbody")
    (base / "no-description").mkdir()
    (base / "no-description" / "SKILL.md").write_text("---\nname: x\n---\nbody")
    _skill(base / "good", "good", "Works")

    assert [s.name for s in project_context.discover_skills(repo)] == ["good"]


def test_prompt_lists_skills_by_name_description_and_path_only(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _skill(
        repo / ".agents" / "skills" / "deploy", "deploy", "Ship a release", body="SECRET STEPS"
    )
    monkeypatch.setenv("RHO_AGENT_SKILLS", "1")

    prompt = Agent(
        AgentConfig(system_prompt="base", model="m", working_dir=str(repo))
    ).system_prompt

    assert f"- deploy: Ship a release ({path})" in prompt
    assert "SECRET STEPS" not in prompt  # the body is read on demand, not preloaded


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------


async def _view(path: Path) -> ToolOutput:
    return await ViewImageHandler().handle(
        ToolInvocation(call_id="v", tool_name="view_image", arguments={"path": str(path)})
    )


async def test_view_image_returns_the_encoded_file(tmp_path: Path) -> None:
    path = tmp_path / "shot.png"
    path.write_bytes(PNG)

    out = await _view(path)

    assert out.success and "Image attached" in out.content
    (image,) = out.images
    assert image["media_type"] == "image/png"
    assert base64.b64decode(image["data"]) == PNG
    assert image["data"] not in out.content  # the text result stays small


async def test_view_image_rejects_missing_unsupported_and_empty_files(tmp_path: Path) -> None:
    (tmp_path / "doc.pdf").write_bytes(b"%PDF")
    (tmp_path / "empty.png").write_bytes(b"")

    assert not (await _view(tmp_path / "nope.png")).success
    assert "Not a supported image type" in (await _view(tmp_path / "doc.pdf")).content
    assert "empty" in (await _view(tmp_path / "empty.png")).content


async def test_oversized_image_is_refused_when_it_cannot_be_shrunk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(view_image, "MAX_IMAGE_BYTES", 10)
    monkeypatch.setattr(view_image, "_shrink", lambda data: None)
    path = tmp_path / "big.png"
    path.write_bytes(PNG)

    out = await _view(path)

    assert not out.success and "too large" in out.content and not out.images


async def test_oversized_image_is_scaled_down_when_pillow_is_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image_module = pytest.importorskip("PIL.Image")
    path = tmp_path / "big.png"
    image_module.effect_noise((3000, 2000), 64).save(path)
    monkeypatch.setattr(view_image, "MAX_IMAGE_BYTES", 200_000)
    assert path.stat().st_size > 200_000

    out = await _view(path)

    assert out.success and "scaled down" in out.content
    assert out.images[0]["media_type"] == "image/jpeg"
    assert len(base64.b64decode(out.images[0]["data"])) <= 200_000


async def test_read_hands_an_image_file_to_the_image_path(tmp_path: Path) -> None:
    path = tmp_path / "plot.png"
    path.write_bytes(PNG)

    out = await ReadHandler().handle(
        ToolInvocation(call_id="r", tool_name="read", arguments={"path": str(path)})
    )

    assert out.success and len(out.images) == 1


class _ImageTool(ToolHandler):
    """Returns one image per call; ``parallel`` controls how the session runs it."""

    def __init__(self, parallel: bool) -> None:
        self._parallel = parallel

    @property
    def parallel_safe(self) -> bool:
        return self._parallel

    @property
    def name(self) -> str:
        return "snap"

    @property
    def description(self) -> str:
        return "Take a picture"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"path": {"type": "string"}}}

    async def handle(self, invocation: ToolInvocation) -> ToolOutput:
        path = invocation.arguments["path"]
        return view_image.encode_image(path, PNG)


class _Client:
    def __init__(self, *calls: list[StreamEvent]) -> None:
        self._calls = list(calls)
        self.prompts: list[Any] = []

    async def stream(self, prompt: Any):
        self.prompts.append(prompt)
        for event in self._calls.pop(0):
            yield event

    async def complete_prompt(self, prompt: Any) -> tuple[str, dict[str, Any]]:
        return "SUMMARY", {"input_tokens": 1, "output_tokens": 1}


def _snap(call_id: str, path: str) -> StreamEvent:
    return StreamEvent(
        type="tool_call", tool_call=ToolCall(id=call_id, name="snap", arguments={"path": path})
    )


def _done() -> StreamEvent:
    return StreamEvent(type="done", usage={"input_tokens": 10, "output_tokens": 2})


def _image_session(client: _Client, *, parallel: bool, trace: Path | None = None) -> Session:
    registry = ToolRegistry()
    registry.register(_ImageTool(parallel))
    agent = MagicMock(spec=Agent)
    agent.config = AgentConfig(system_prompt="sys", access="full", model="test-model")
    agent.system_prompt = "sys"
    agent.registry = registry
    agent.sandbox_manager = None
    session = Session(agent, client=client, state=State(trace_path=trace))
    session.parallel_tool_calls = parallel
    return session


@pytest.mark.parametrize("parallel", [False, True])
async def test_images_follow_all_of_a_turns_tool_results(parallel: bool) -> None:
    client = _Client(
        [_snap("a", "/tmp/a.png"), _snap("b", "/tmp/b.png"), _done()],
        [StreamEvent(type="text", content="I see them"), _done()],
    )
    session = _image_session(client, parallel=parallel)

    result = await session.run("look")

    assert result.status == "completed"
    roles = [m["role"] for m in session.state.messages]
    assert roles == ["user", "assistant", "tool", "tool", "user", "assistant"]
    parts = session.state.messages[4]["content"]
    assert parts[0]["text"].startswith(IMAGE_ATTACHMENT_PREFIX)
    assert "/tmp/a.png, /tmp/b.png" in parts[0]["text"]
    assert [p["type"] for p in parts[1:]] == ["image_url", "image_url"]
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
    # The model received the images on its next call.
    sent = client.prompts[1].messages[-1]
    assert sent.role == "user" and sent.content == parts


async def test_an_image_counts_as_a_fixed_token_cost_not_its_base64_text() -> None:
    client = _Client(
        [_snap("a", "/tmp/a.png"), _done()], [StreamEvent(type="text", content="ok"), _done()]
    )
    session = _image_session(client, parallel=False)
    await session.run("look")
    big = "A" * 4_000_000  # a 3 MB image as base64
    session.state.messages[3]["content"][1]["image_url"]["url"] = f"data:image/png;base64,{big}"

    estimate = session.state.estimate_tokens("sys")

    assert IMAGE_TOKEN_ESTIMATE <= estimate < IMAGE_TOKEN_ESTIMATE + 500


async def test_compaction_trims_old_images_and_does_not_keep_them_as_user_speech() -> None:
    calls = [[_snap(f"c{i}", f"/tmp/{i}.png"), _done()] for i in range(8)]
    client = _Client(*calls, [StreamEvent(type="text", content="done"), _done()])
    session = _image_session(client, parallel=False)
    await session.run("the task")
    session.compact_at_tokens = 12_000  # tail budget = 3000 tokens, about two images

    result = await session.compact(trigger="auto")

    assert result.method == "prune"
    messages = session.state.messages
    stubs = [m for m in messages if isinstance(m["content"], str) and "removed" in m["content"]]
    kept = [m for m in messages if isinstance(m["content"], list)]
    assert len(stubs) >= 5 and len(kept) >= 1  # recent images stay, old ones go
    assert all("/tmp/" in m["content"] for m in stubs)  # still says which file it was

    # A summary round keeps the task, but not image messages, as the user's words.
    session.compact_at_tokens = 400  # small tail, so the stubs fall in the summarised part
    result = await session.compact()
    assert result.method == "summary" and result.error is None
    users = [m["content"] for m in session.state.messages if m["role"] == "user"]
    assert users[0] == "the task" and "SUMMARY" in users[1]


async def test_images_survive_resume_and_export_as_a_marker(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    client = _Client(
        [_snap("a", "/tmp/a.png"), _done()], [StreamEvent(type="text", content="ok"), _done()]
    )
    session = _image_session(client, parallel=False, trace=trace)
    await session.run("look")

    resumed = State.from_jsonl(trace.read_bytes())
    assert resumed.messages == session.state.messages

    trajectory = trace_to_atif(trace, session_id="s")
    step = next(s for s in trajectory["steps"] if "[1 image attached]" in str(s["message"]))
    assert "base64" not in json.dumps(step)
    harbor_models = pytest.importorskip("harbor.models.trajectories")
    harbor_models.Trajectory.model_validate(trajectory)


# ---------------------------------------------------------------------------
# The view_image switch
# ---------------------------------------------------------------------------


async def test_view_image_off_removes_the_tool_and_stops_read_attaching(tmp_path: Path) -> None:
    from rho_agent.tools.factory import build_registry

    registry = build_registry("full", tools=["read", "bash"])
    path = tmp_path / "plot.png"
    path.write_bytes(PNG)

    out = await registry.dispatch(
        ToolInvocation(call_id="r", tool_name="read", arguments={"path": str(path)})
    )

    assert "view_image" not in registry._handlers
    assert not out.success and not out.images  # treated as a binary file again


def test_bash_with_view_image_gives_exactly_two_tools() -> None:
    from rho_agent.tools.factory import build_registry

    assert set(build_registry("full", tools=["bash", "view_image"])._handlers) == {
        "bash",
        "view_image",
    }
