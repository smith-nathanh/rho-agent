# SQL Explorer Demo

A Streamlit web app demonstrating rho-agent's database exploration capabilities. Chat with an AI agent to explore a SQLite database, then export queries and results.

## Setup

Requires an OpenAI-compatible API key. Create a `.env` file in the **project root** (not in this directory):

```bash
OPENAI_API_KEY=sk-...

# Optional: alternative provider
OPENAI_BASE_URL=https://api.together.xyz/v1
```

Install the `dashboard` extra for Streamlit:

```bash
uv sync --group dev --extra dashboard
```

## Quick Start

```bash
# 1. Seed the sample database (one-time)
uv run python examples/sql_explorer/seed_database.py

# 2. Launch the app
uv run streamlit run examples/sql_explorer/app.py
```

The app opens at http://localhost:8501

## Features

### Agent Chat

Ask questions in natural language and watch the agent explore the database in real-time:

- "What tables are available?"
- "Show me the top 5 highest paid employees"
- "Which department has the most projects?"

The agent streams its work as it happens—you'll see each tool call execute and return results.

### SQL Editor

Write and run your own SQL queries:

- Execute queries directly against the database
- View results in a table
- Export to CSV

### Bidirectional Workflow

The app supports iterating between the agent and manual SQL:

**Agent → SQL Editor**: When the agent runs a query, click "Open in SQL Editor" to copy it. Then tweak it, run it yourself, and export results.

**SQL Editor → Agent**: Have a complex query you need help with? Click "Send to Agent" to get assistance debugging, optimizing, or explaining it.

## Sample Database

The `seed_database.py` script creates a sample company database with:

| Table | Description |
|-------|-------------|
| `employees` | Employee records with name, email, salary, hire date |
| `departments` | Department names and budgets |
| `projects` | Projects with status, deadlines, assigned departments |
| `timesheets` | Employee time entries on projects |

## Architecture

The demo uses rho-agent's core components:

- `Agent` - Stateless agent definition (config + tool registry)
- `Session` - Execution context driving the conversation loop
- `State` - Conversation history and usage tracking
- `SqliteHandler` - Provides database access (readonly mode)
- `ModelClient` - Streams responses from the LLM

See [ARCHITECTURE.md](../ARCHITECTURE.md) for details on the agent harness.
