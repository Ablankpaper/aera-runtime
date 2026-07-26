"""Aera read-only context must not weaken Hermes private self-evolution."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from hermes_state import SessionDB
from run_agent import AIAgent


GLOBAL_CONTEXT_SENTINEL = "AERA_GLOBAL_CONTEXT_SENTINEL_7f6f5b3d"
PRIVATE_MEMORY_SENTINEL = "Hermes learned this private workflow preference."
USER_MESSAGE = "Please remember my private workflow preference and answer normally."


def _response(*, content: str, finish_reason: str, tool_calls=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], model="test/model", usage=None)


def _memory_add_call():
    return SimpleNamespace(
        id="call_private_memory",
        type="function",
        function=SimpleNamespace(
            name="memory",
            arguments=json.dumps(
                {
                    "action": "add",
                    "target": "memory",
                    "content": PRIVATE_MEMORY_SENTINEL,
                }
            ),
        ),
    )


def _all_text(messages) -> str:
    return "\n".join(
        str(message.get("content", ""))
        for message in messages
        if isinstance(message, dict)
    )


def test_ephemeral_global_context_stays_out_of_learning_inputs_while_private_learning_runs(
    tmp_path,
):
    """The Aera block is request-only; native persistence and review still run."""
    hermes_home = Path(os.environ["HERMES_HOME"])
    (hermes_home / "config.yaml").write_text(
        """
memory:
  memory_enabled: true
  user_profile_enabled: true
  nudge_interval: 1
skills:
  creation_nudge_interval: 0
compression:
  enabled: false
""".strip()
        + "\n",
        encoding="utf-8",
    )

    session_id = "aera-ephemeral-self-evolution"
    session_db = SessionDB(db_path=tmp_path / "state.db")
    with patch("run_agent.OpenAI"):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            provider="openrouter",
            model="test/model",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=False,
            enabled_toolsets=["memory"],
            ephemeral_system_prompt=GLOBAL_CONTEXT_SENTINEL,
            session_id=session_id,
            session_db=session_db,
        )

    outbound_requests = []
    responses = iter(
        [
            _response(
                content="",
                finish_reason="tool_calls",
                tool_calls=[_memory_add_call()],
            ),
            _response(content="Done.", finish_reason="stop"),
        ]
    )

    def fake_api_call(api_kwargs):
        outbound_requests.append(api_kwargs)
        return next(responses)

    agent._interruptible_api_call = fake_api_call
    agent._cached_system_prompt = "BASE_SYSTEM_PROMPT"
    agent.compression_enabled = False
    agent.tool_delay = 0
    agent._use_prompt_caching = False
    agent._spawn_background_review = MagicMock()

    result = agent.run_conversation(USER_MESSAGE)

    assert result["completed"] is True
    assert result["final_response"] == "Done."
    assert "memory" in agent.valid_tool_names

    assert len(outbound_requests) == 2
    outbound_text = _all_text(outbound_requests[0]["messages"])
    assert GLOBAL_CONTEXT_SENTINEL in outbound_text
    assert USER_MESSAGE in outbound_text

    result_text = _all_text(result["messages"])
    assert USER_MESSAGE in result_text
    assert GLOBAL_CONTEXT_SENTINEL not in result_text

    persisted_messages = session_db.get_messages_as_conversation(session_id)
    persisted_text = _all_text(persisted_messages)
    assert USER_MESSAGE in persisted_text
    assert GLOBAL_CONTEXT_SENTINEL not in persisted_text

    agent._spawn_background_review.assert_called_once()
    review_call = agent._spawn_background_review.call_args
    assert review_call.kwargs["review_memory"] is True
    review_text = _all_text(review_call.kwargs["messages_snapshot"])
    assert USER_MESSAGE in review_text
    assert GLOBAL_CONTEXT_SENTINEL not in review_text

    memory_path = hermes_home / "memories" / "MEMORY.md"
    memory_text = memory_path.read_text(encoding="utf-8")
    assert PRIVATE_MEMORY_SENTINEL in memory_text
    assert GLOBAL_CONTEXT_SENTINEL not in memory_text

    user_memory_path = hermes_home / "memories" / "USER.md"
    if user_memory_path.exists():
        assert GLOBAL_CONTEXT_SENTINEL not in user_memory_path.read_text(encoding="utf-8")
