from rho_agent.core.agent import AgentEvent
from rho_agent.eval.harbor.trajectory import TrajectoryBuilder


def test_step_metrics_aggregate_multiple_api_calls() -> None:
    builder = TrajectoryBuilder(model="gpt-5-mini")
    events = [
        AgentEvent(
            type="api_call_complete",
            usage={
                "input_tokens": 100,
                "output_tokens": 40,
                "cached_tokens": 10,
                "reasoning_tokens": 5,
                "cost_usd": 0.01,
            },
        ),
        AgentEvent(
            type="api_call_complete",
            usage={
                "input_tokens": 30,
                "output_tokens": 20,
                "cached_tokens": 0,
                "reasoning_tokens": 2,
                "cost_usd": 0.005,
            },
        ),
        AgentEvent(type="text", content="done"),
        AgentEvent(
            type="turn_complete",
            usage={
                "total_input_tokens": 130,
                "total_output_tokens": 60,
                "total_cached_tokens": 10,
                "total_reasoning_tokens": 7,
                "total_cost_usd": 0.015,
                "context_size": 130,
            },
        ),
    ]

    builder.build_from_events(events, user_input="task")
    trajectory = builder.to_trajectory()

    agent_step = trajectory["steps"][1]
    assert agent_step["metrics"]["prompt_tokens"] == 130
    assert agent_step["metrics"]["completion_tokens"] == 60
    assert agent_step["metrics"]["cached_tokens"] == 10
    assert agent_step["metrics"]["cost_usd"] == 0.015
    assert agent_step["metrics"]["extra"]["reasoning_tokens"] == 7
