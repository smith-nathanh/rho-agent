# ro-agent

A read-only research agent implemented in Python for inspecting logs, files, and databases.

## Usage

```bash
# Interactive mode
uv run ro-agent

# Single prompt
uv run ro-agent "what's in the logs directory?"

# Auto-approve all commands (no prompts)
uv run ro-agent --auto-approve "inspect the error logs"

# Custom model/endpoint (vLLM, etc.)
uv run ro-agent --base-url http://localhost:8000/v1 --model qwen2.5-72b
```

### Interactive Example

```
> what's in the logs directory?

▶ shell: ls -la /var/log
drwxr-xr-x  12 root  wheel  384 Dec 21 10:00 .
-rw-r--r--   1 root  wheel  1024 Dec 21 09:00 system.log
...

There are 12 files in /var/log. The main ones are...

> show me recent errors in system.log

▶ shell: grep -i error /var/log/system.log | tail -20
...

I found 3 recent errors related to...

> exit
```

## Configuration

Create a `.env` file:

```bash
OPENAI_API_KEY=your-key-here
OPENAI_BASE_URL=http://your-vllm-server:8000/v1  # optional
OPENAI_MODEL=gpt-4o  # optional
```

## Safety

- **Command allowlist**: Only safe read-only commands allowed (ls, cat, grep, etc.)
- **Dangerous pattern blocking**: Rejects rm, sudo, redirects, etc.
- **Approval prompts**: Confirm each command, or use `--auto-approve`

## Adding Tools

Implement `ToolHandler` and register with `ToolRegistry`:

```python
class MyHandler(ToolHandler):
    @property
    def name(self) -> str: ...
    @property
    def description(self) -> str: ...
    @property
    def parameters(self) -> dict: ...
    async def handle(self, invocation: ToolInvocation) -> ToolOutput: ...
```
