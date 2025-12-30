# ro-agent

A read-only research agent for compute clusters. Assists developers by inspecting logs, probing database schemas, and finding relevant documentation—without modifying anything.

## Installation

```bash
uv sync
```

## Usage

```bash
# Interactive mode
uv run ro-agent

# Single prompt (agent completes task and exits)
uv run ro-agent "why did job 12345 fail?"

# With working directory context
uv run ro-agent --working-dir /data/jobs/12345/logs "find the errors"

# Auto-approve shell commands
uv run ro-agent --auto-approve "inspect the error logs"

# Custom model/endpoint
uv run ro-agent --base-url http://localhost:8000/v1 --model qwen2.5-72b
```

## Tools

The agent has four built-in tools, modeled after Claude Code's patterns:

### `read_file`
Read file contents with optional line ranges.
```
read_file(path="/path/to/file.log", start_line=100, end_line=200)
```

### `list_dir`
List directory contents (flat or recursive tree).
```
list_dir(path="/data/logs", recursive=true, max_depth=2)
```

### `grep_files`
Search for patterns in files. Three output modes to control context usage:

| Mode | Description | Use Case |
|------|-------------|----------|
| `files_with_matches` (default) | Returns only file paths | Discover which files match |
| `content` | Returns matching lines with context | See actual matches |
| `count` | Returns match counts per file | Gauge match distribution |

```
# First, find which files match
grep_files(pattern="ERROR", path="/logs", glob="*.log")
# Output: /logs/app.log, /logs/worker.log [2 matching files]

# Then drill into specific file
grep_files(pattern="ERROR", path="/logs/app.log", output_mode="content", context_lines=3)
```

### `shell`
Execute shell commands (requires approval). Allowlisted to safe read-only commands.
```
shell(command="jq '.errors' /data/results.json")
```

## Safety

- **Dedicated read-only tools**: `read_file`, `list_dir`, `grep_files` run without approval
- **Shell allowlist**: Only safe commands allowed (grep, cat, jq, etc.)
- **Dangerous pattern blocking**: Rejects rm, sudo, redirects, etc.
- **Approval prompts**: Shell commands require confirmation (use `--auto-approve` to skip)
- **Output truncation**: Large outputs are truncated to prevent context overflow

## Configuration

Create a `.env` file:

```bash
OPENAI_API_KEY=your-key-here
OPENAI_BASE_URL=http://your-vllm-server:8000/v1  # optional
OPENAI_MODEL=gpt-4o  # optional, defaults to gpt-5-nano
```

History is stored at `~/.config/ro-agent/history`.

## Architecture

```
ro_agent/
├── cli.py              # Entry point, REPL, event handling
├── core/
│   ├── agent.py        # Main agent loop (prompt → model → tools → loop)
│   └── session.py      # Conversation history management
├── client/
│   └── model.py        # OpenAI-compatible streaming client
├── tools/
│   ├── base.py         # ToolHandler ABC
│   ├── registry.py     # Tool registration and dispatch
│   └── handlers/
│       ├── read_file.py
│       ├── list_dir.py
│       ├── grep_files.py
│       └── shell.py
└── mcp/                # MCP integration (planned)
```

Based on [Codex CLI](https://github.com/openai/codex) architecture patterns.

## Adding Tools

Implement `ToolHandler` and register in `cli.py`:

```python
from ro_agent.tools.base import ToolHandler, ToolInvocation, ToolOutput

class MyHandler(ToolHandler):
    @property
    def name(self) -> str:
        return "my_tool"

    @property
    def description(self) -> str:
        return "What this tool does"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "arg1": {"type": "string", "description": "..."},
            },
            "required": ["arg1"],
        }

    @property
    def requires_approval(self) -> bool:
        return False  # True for potentially dangerous tools

    async def handle(self, invocation: ToolInvocation) -> ToolOutput:
        # Do the work
        return ToolOutput(content="result", success=True)
```

## Interactive Commands

| Command | Description |
|---------|-------------|
| `/approve` | Enable auto-approve for session |
| `/help` | Show help |
| `/clear` | Clear screen |
| `exit` | Quit |
