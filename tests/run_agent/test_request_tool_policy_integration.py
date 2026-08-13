"""Integration coverage for the signed per-request tool policy boundary."""

from types import SimpleNamespace
from unittest.mock import patch

from gateway.platforms.api_server import _parse_request_tool_policy
from run_agent import AIAgent


def _tool_call(name: str) -> SimpleNamespace:
    return SimpleNamespace(
        id="policy-integration-call",
        type="function",
        function=SimpleNamespace(name=name, arguments="{}"),
    )


def test_parsed_request_policy_filters_real_tools_and_blocks_stale_execution():
    """Parse, initialize, resolve, and execute under one fail-closed policy."""
    policy = _parse_request_tool_policy(
        {
            "allowed": ["read_file"],
            "denied": ["image_generate"],
        }
    )

    with patch("run_agent.OpenAI"):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://example.test/v1",
            model="test-model",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            enabled_toolsets=["file", "image_gen"],
            request_tool_policy=policy,
        )

    assert agent.valid_tool_names == {"read_file"}

    # Simulate stale runtime metadata after initialization. The execution
    # boundary must still apply the parsed request policy before dispatch.
    agent.valid_tool_names.add("image_generate")
    messages = []
    assistant_message = SimpleNamespace(
        content="",
        tool_calls=[_tool_call("image_generate")],
    )

    with patch("run_agent.handle_function_call") as dispatch:
        agent._execute_tool_calls_sequential(
            assistant_message,
            messages,
            "policy-integration-task",
        )

    dispatch.assert_not_called()
    assert len(messages) == 1
    assert "not available in this session" in messages[0]["content"]
