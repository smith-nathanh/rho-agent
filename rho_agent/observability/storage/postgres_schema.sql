-- PostgreSQL schema for rho-agent telemetry
-- Apply with: psql < postgres_schema.sql

-- Sessions table: one row per agent invocation
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    agent_id TEXT,
    environment TEXT,
    profile TEXT,
    model TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ,
    status TEXT DEFAULT 'active',
    total_input_tokens INTEGER DEFAULT 0,
    total_output_tokens INTEGER DEFAULT 0,
    total_reasoning_tokens INTEGER DEFAULT 0,
    total_tool_calls INTEGER DEFAULT 0,
    context_size INTEGER DEFAULT 0,
    metadata JSONB
);

-- Turns table: one row per user input/response cycle
CREATE TABLE IF NOT EXISTS turns (
    turn_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    turn_index INTEGER NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0,
    context_size INTEGER DEFAULT 0,
    user_input TEXT
);

-- Tool executions table: one row per tool call
CREATE TABLE IF NOT EXISTS tool_executions (
    execution_id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL REFERENCES turns(turn_id),
    tool_name TEXT NOT NULL,
    arguments JSONB,
    result TEXT,
    success BOOLEAN DEFAULT TRUE,
    error TEXT,
    duration_ms INTEGER DEFAULT 0,
    started_at TIMESTAMPTZ NOT NULL
);

-- Agent registry: tracks running agents across nodes
CREATE TABLE IF NOT EXISTS agent_registry (
    session_id TEXT PRIMARY KEY,
    pid INTEGER NOT NULL,
    model TEXT NOT NULL,
    instruction_preview TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status TEXT NOT NULL DEFAULT 'running',
    labels JSONB DEFAULT '{}'::JSONB
);

-- Signal queue: cross-node agent control signals
CREATE TABLE IF NOT EXISTS signal_queue (
    id SERIAL PRIMARY KEY,
    session_id TEXT NOT NULL,
    signal_type TEXT NOT NULL,  -- cancel, pause, resume, directive
    payload TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    consumed_at TIMESTAMPTZ
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_sessions_team_project ON sessions(team_id, project_id);
CREATE INDEX IF NOT EXISTS idx_sessions_started_at ON sessions(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
CREATE INDEX IF NOT EXISTS idx_sessions_metadata ON sessions USING GIN (metadata);
CREATE INDEX IF NOT EXISTS idx_turns_session_id ON turns(session_id);
CREATE INDEX IF NOT EXISTS idx_tool_executions_turn_id ON tool_executions(turn_id);
CREATE INDEX IF NOT EXISTS idx_tool_executions_tool_name ON tool_executions(tool_name);
CREATE INDEX IF NOT EXISTS idx_agent_registry_heartbeat ON agent_registry(heartbeat_at);
CREATE INDEX IF NOT EXISTS idx_agent_registry_labels ON agent_registry USING GIN (labels);
CREATE INDEX IF NOT EXISTS idx_signal_queue_session ON signal_queue(session_id, consumed_at);

-- LISTEN/NOTIFY triggers for real-time telemetry events (one function per table
-- because Postgres evaluates all CASE branches against NEW, which fails when
-- a column doesn't exist on the triggering table).

CREATE OR REPLACE FUNCTION rho_agent_notify_session() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('rho_agent_events', json_build_object(
        'table', 'sessions', 'op', TG_OP, 'id', NEW.session_id
    )::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION rho_agent_notify_turn() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('rho_agent_events', json_build_object(
        'table', 'turns', 'op', TG_OP, 'id', NEW.turn_id
    )::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION rho_agent_notify_tool_execution() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('rho_agent_events', json_build_object(
        'table', 'tool_executions', 'op', TG_OP, 'id', NEW.execution_id
    )::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_sessions_notify') THEN
        CREATE TRIGGER trg_sessions_notify
            AFTER INSERT OR UPDATE ON sessions
            FOR EACH ROW EXECUTE FUNCTION rho_agent_notify_session();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_turns_notify') THEN
        CREATE TRIGGER trg_turns_notify
            AFTER INSERT OR UPDATE ON turns
            FOR EACH ROW EXECUTE FUNCTION rho_agent_notify_turn();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_tool_executions_notify') THEN
        CREATE TRIGGER trg_tool_executions_notify
            AFTER INSERT ON tool_executions
            FOR EACH ROW EXECUTE FUNCTION rho_agent_notify_tool_execution();
    END IF;
END;
$$;

-- NOTIFY trigger for signal queue (per-session channel)
CREATE OR REPLACE FUNCTION rho_agent_notify_signal() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('rho_agent_signal_' || NEW.session_id, json_build_object(
        'signal_type', NEW.signal_type,
        'payload', NEW.payload,
        'id', NEW.id
    )::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_signal_queue_notify') THEN
        CREATE TRIGGER trg_signal_queue_notify
            AFTER INSERT ON signal_queue
            FOR EACH ROW EXECUTE FUNCTION rho_agent_notify_signal();
    END IF;
END;
$$;
