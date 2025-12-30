# ro-agent

A read-only research agent for searching directories, inspecting files, and exploring code or databases—without modifying anything.

## Installation

```bash
uv sync
```

## Usage

```bash
# Interactive mode
uv run ro-agent

# Single prompt (agent completes task and exits)
uv run ro-agent "what does this project do?"

# With working directory context
uv run ro-agent --working-dir ~/proj/myapp "find the error handling code"

# Auto-approve shell commands
uv run ro-agent --auto-approve "inspect the logs"

# Custom model/endpoint
uv run ro-agent --base-url http://localhost:8000/v1 --model qwen2.5-72b
```

## Interactive Example

```
╭──────────────────────────────────────────────────────────────────╮
│ ro-agent - Read-only research assistant                          │
│ Model: gpt-5-nano                                                │
│ Type /help for commands, exit to quit.                           │
╰──────────────────────────────────────────────────────────────────╯

> What's in ~/proj/myapp?

╭────────────────────────────────── list_dir ──────────────────────────────────╮
│ {'path': '/home/user/proj/myapp', 'show_hidden': False}                      │
╰──────────────────────────────────────────────────────────────────────────────╯
╭──────────────────────────────────────────────────────────────────────────────╮
│ drwxr-xr-x         -  2025-12-07 15:46  src/                                 │
│ drwxr-xr-x         -  2025-12-07 15:46  tests/                               │
│ -rw-r--r--      6739  2025-12-07 15:47  main.py                              │
│ -rw-r--r--      3536  2025-12-07 16:26  README.md                            │
│ -rw-r--r--       757  2025-12-07 15:48  pyproject.toml                       │
╰──────────────────────────────────────────────────────────────────────────────╯

> Find files with "error" in them

╭────────────────────────────────── grep_files ────────────────────────────────╮
│ {'pattern': 'error', 'path': '/home/user/proj/myapp', 'glob': '*.py'}        │
╰──────────────────────────────────────────────────────────────────────────────╯
╭──────────────────────────────────────────────────────────────────────────────╮
│ /home/user/proj/myapp/src/api.py                                             │
│ /home/user/proj/myapp/src/handlers.py                                        │
│                                                                              │
│ [2 matching files]                                                           │
╰──────────────────────────────────────────────────────────────────────────────╯

> Show me the error handling in api.py

╭────────────────────────────────── grep_files ────────────────────────────────╮
│ {'pattern': 'error', 'path': '/home/user/proj/myapp/src/api.py',             │
│  'output_mode': 'content', 'context_lines': 2}                               │
╰──────────────────────────────────────────────────────────────────────────────╯
╭──────────────────────────────────────────────────────────────────────────────╮
│ ── /home/user/proj/myapp/src/api.py ──                                       │
│      41      try:                                                            │
│      42          response = self.client.request(endpoint)                    │
│ >    43      except Exception as error:                                      │
│      44          logger.warning(f"Request failed: {error}")                  │
│      45          return None                                                 │
│                                                                              │
│ [1 matches in 1 files]                                                       │
╰──────────────────────────────────────────────────────────────────────────────╯

The error handling in api.py catches exceptions from client requests
and logs a warning before returning None...

[1247 in, 156 out]

> exit
```

## Tools

Four built-in tools, modeled after Claude Code's patterns:

### `list_dir`
Explore directory structures with flat or recursive tree views.
```
list_dir(path="/data/logs")                           # flat listing
list_dir(path="/project", recursive=true, max_depth=3) # tree view
list_dir(path="/project", show_hidden=true)           # include dotfiles
```

### `grep_files`
Search for patterns across directory trees. Three output modes to control context usage:

| Mode | Description | Use Case |
|------|-------------|----------|
| `files_with_matches` (default) | Returns only file paths | Discover which files match |
| `content` | Returns matching lines with context | See actual matches |
| `count` | Returns match counts per file | Gauge match distribution |

```
# Find all Python files containing "TODO"
grep_files(pattern="TODO", path="/project/src", glob="*.py")

# Search logs for errors, see surrounding context
grep_files(pattern="ERROR|FATAL", path="/var/log", glob="*.log",
           output_mode="content", context_lines=3)

# Count matches per file
grep_files(pattern="import", path="/project", glob="*.py", output_mode="count")
```

### `read_file`
Read file contents with optional line ranges.
```
read_file(path="/path/to/file.py")                    # full file (up to 500 lines)
read_file(path="/path/to/file.py", start_line=100, end_line=200)  # specific range
```

### `shell`
Execute shell commands (requires approval). Allowlisted to safe read-only commands.
```
shell(command="jq '.errors' /data/results.json")
shell(command="wc -l *.py")
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
OPENAI_MODEL=gpt-4o  # optional
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
| `/compact [guidance]` | Compact conversation history (see below) |
| `/help` | Show help |
| `/clear` | Clear screen |
| `exit` | Quit |

## Context Management

ro-agent includes compaction features to manage long conversations:

### Manual Compaction
Use `/compact` to summarize the conversation when context gets long:
```
> /compact
Compacting conversation...
Compacted: 45000 → 3200 tokens

> /compact focus on the database schema findings
Compacting conversation...
Compacted: 32000 → 2800 tokens
```

### Auto-Compaction
When context approaches 80% of the limit (default 100k tokens), ro-agent automatically compacts before processing your next message:
```
Context limit approaching, auto-compacting...
Compacted: 82000 → 4500 tokens
```

The compaction creates a handoff summary that preserves:
- Progress and key decisions made
- Important context and user preferences
- Next steps and remaining work
- Critical file paths and references
