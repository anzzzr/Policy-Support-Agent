"""
Tests for the agent loop using a fake LLM — no network access needed.

The FakeLLM returns pre-scripted responses so we can test:
  - normal tool-call flows modify the DB correctly
  - guard violations are captured in the trace
  - the MAX_ROUNDS safety net fires
  - parallel tool calls in one response
"""

import pytest

from app.db import get_db, seed, get_order
from app.agent import run_turn, build_system_prompt, MAX_ROUNDS


# ── Fake LLM ──────────────────────────────────────────────────────────

class FakeLLM:
    """Returns pre-scripted responses, one per chat() call."""

    def __init__(self, responses: list[dict]):
        self._responses = iter(responses)
        self.call_count = 0

    def __call__(self, messages, tools):
        self.call_count += 1
        return next(self._responses)


def _tool_call(call_id: str, name: str, args: dict) -> dict:
    """Build a fake assistant response containing one tool call."""
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": args},
            }
        ],
    }


def _text_reply(text: str) -> dict:
    """Build a fake assistant text response (no tool calls)."""
    return {"role": "assistant", "content": text}


# ── Fixtures ──────────────────────────────────────────────────────────

@pytest.fixture
def db():
    conn = get_db(":memory:")
    seed(conn)
    yield conn
    conn.close()


# ── Agent-loop tests ─────────────────────────────────────────────────

class TestAgentLoop:

    def test_cancel_order_flow(self, db):
        """Full flow: lookup → cancel → text.  DB should update."""
        fake = FakeLLM([
            _tool_call("c1", "lookup_order",
                        {"order_id": 1001, "email": "aarav@example.com"}),
            _tool_call("c2", "cancel_order", {"order_id": 1001}),
            _text_reply("Your order 1001 has been cancelled."),
        ])

        messages = [{"role": "system", "content": "test"}]
        messages.append({"role": "user", "content": "Cancel my order 1001"})
        trace = []

        reply = run_turn(db, messages, trace, chat_fn=fake)

        assert reply == "Your order 1001 has been cancelled."
        assert get_order(db, 1001)["status"] == "cancelled"
        assert len(trace) == 2
        assert trace[0]["tool"] == "lookup_order"
        assert trace[0]["blocked"] is False
        assert trace[1]["tool"] == "cancel_order"
        assert trace[1]["blocked"] is False

    def test_guard_blocks_violation_and_records_in_trace(self, db):
        """Cancel on a shipped order → guard blocks, trace shows blocked."""
        fake = FakeLLM([
            _tool_call("c1", "cancel_order", {"order_id": 1002}),
            _text_reply("Sorry, I cannot cancel a shipped order."),
        ])

        messages = [{"role": "system", "content": "test"}]
        messages.append({"role": "user", "content": "Cancel order 1002"})
        trace = []

        reply = run_turn(db, messages, trace, chat_fn=fake)

        assert "cannot cancel" in reply.lower()
        assert get_order(db, 1002)["status"] == "shipped"   # unchanged
        assert len(trace) == 1
        assert trace[0]["blocked"] is True

    def test_max_rounds_exceeded(self, db):
        """LLM that never stops calling tools → bail after MAX_ROUNDS."""
        responses = [
            _tool_call(f"c{i}", "lookup_order",
                        {"order_id": 1001, "email": "aarav@example.com"})
            for i in range(MAX_ROUNDS + 5)      # more than enough
        ]
        fake = FakeLLM(responses)

        messages = [{"role": "system", "content": "test"}]
        messages.append({"role": "user", "content": "Help me"})
        trace = []

        reply = run_turn(db, messages, trace, chat_fn=fake)

        assert "maximum" in reply.lower()
        assert fake.call_count == MAX_ROUNDS

    def test_text_reply_without_tools(self, db):
        """LLM replies with text on the first call — no tools run."""
        fake = FakeLLM([_text_reply("Hello! How can I help you?")])

        messages = [{"role": "system", "content": "test"}]
        messages.append({"role": "user", "content": "Hi"})
        trace = []

        reply = run_turn(db, messages, trace, chat_fn=fake)

        assert reply == "Hello! How can I help you?"
        assert trace == []
        assert fake.call_count == 1

    def test_multiple_tool_calls_in_one_response(self, db):
        """Some LLMs return several tool calls at once."""
        fake = FakeLLM([
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1", "type": "function",
                        "function": {
                            "name": "lookup_order",
                            "arguments": {"order_id": 1001,
                                          "email": "aarav@example.com"},
                        },
                    },
                    {
                        "id": "c2", "type": "function",
                        "function": {
                            "name": "lookup_order",
                            "arguments": {"order_id": 1003,
                                          "email": "priya@example.com"},
                        },
                    },
                ],
            },
            _text_reply("Found both orders."),
        ])

        messages = [{"role": "system", "content": "test"}]
        messages.append({"role": "user", "content": "Look up two orders"})
        trace = []

        reply = run_turn(db, messages, trace, chat_fn=fake)

        assert reply == "Found both orders."
        assert len(trace) == 2
        assert trace[0]["args"]["order_id"] == 1001
        assert trace[1]["args"]["order_id"] == 1003

    def test_trace_captures_full_detail(self, db):
        """Each trace entry has tool, args, result, and blocked flag."""
        fake = FakeLLM([
            _tool_call("c1", "lookup_order",
                        {"order_id": 1001, "email": "aarav@example.com"}),
            _text_reply("Done."),
        ])

        messages = [{"role": "system", "content": "test"}]
        messages.append({"role": "user", "content": "Look up order"})
        trace = []

        run_turn(db, messages, trace, chat_fn=fake)

        entry = trace[0]
        assert entry["tool"] == "lookup_order"
        assert entry["args"]["order_id"] == 1001
        assert entry["result"]["ok"] is True
        assert entry["result"]["customer_name"] == "Aarav Sharma"
        assert entry["blocked"] is False

    def test_messages_grow_correctly(self, db):
        """After a turn the messages list should contain the full exchange."""
        fake = FakeLLM([
            _tool_call("c1", "lookup_order",
                        {"order_id": 1001, "email": "aarav@example.com"}),
            _text_reply("Found it."),
        ])

        messages = [
            {"role": "system", "content": "test"},
            {"role": "user", "content": "Find order 1001"},
        ]
        trace = []

        run_turn(db, messages, trace, chat_fn=fake)

        roles = [m["role"] for m in messages]
        # system, user, assistant(tool_call), tool(result), assistant(text)
        assert roles == ["system", "user", "assistant", "tool", "assistant"]

    def test_malformed_json_tool_arguments_handled_cleanly(self, db):
        """Malformed JSON string in arguments should not crash the loop."""
        malformed_call = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "bad_call_1",
                    "type": "function",
                    "function": {"name": "lookup_order", "arguments": "{bad: json"},
                }
            ],
        }
        fake = FakeLLM([
            malformed_call,
            _text_reply("I encountered a formatting error with the tool arguments."),
        ])

        messages = [
            {"role": "system", "content": "test"},
            {"role": "user", "content": "lookup order"},
        ]
        trace = []

        reply = run_turn(db, messages, trace, chat_fn=fake)
        assert "formatting error" in reply
        assert len(trace) == 1
        assert trace[0]["result"]["ok"] is False
        assert trace[0]["result"]["success"] is False
        assert "malformed json" in trace[0]["result"]["error"].lower()

        # Verify tool response message was appended with error content
        tool_msg = messages[-2]
        assert tool_msg["role"] == "tool"
        assert "malformed json" in tool_msg["content"].lower()

    def test_unknown_tool_handled_cleanly(self, db):
        """Unknown tool name requested by model should return error result."""
        unknown_tool_call = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "unknown_call_1",
                    "type": "function",
                    "function": {"name": "hack_system", "arguments": {}},
                }
            ],
        }
        fake = FakeLLM([
            unknown_tool_call,
            _text_reply("That action is unsupported."),
        ])

        messages = [
            {"role": "system", "content": "test"},
            {"role": "user", "content": "hack system"},
        ]
        trace = []

        reply = run_turn(db, messages, trace, chat_fn=fake)
        assert reply == "That action is unsupported."
        assert len(trace) == 1
        assert trace[0]["result"]["ok"] is False
        assert trace[0]["result"]["success"] is False
        assert "unknown tool" in trace[0]["result"]["error"].lower()


# ── System-prompt tests ──────────────────────────────────────────────

class TestSystemPrompt:

    def test_includes_full_policy(self):
        prompt = build_system_prompt()
        assert "Verify identity first" in prompt
        assert "final-sale" in prompt.lower() or "final sale" in prompt.lower()
        assert "5,000" in prompt or "5000" in prompt
        assert "escalat" in prompt.lower()

    def test_includes_tool_instructions(self):
        prompt = build_system_prompt()
        assert "order ID" in prompt
        assert "email" in prompt
        assert "lookup_order" in prompt
        assert "check_refund_eligibility" in prompt

    def test_no_scenario_specific_examples_in_system_prompt(self):
        """System prompt must remain general and free of evaluation scenario contamination."""
        prompt = build_system_prompt()
        assert "price matching" not in prompt.lower()
        assert "wallet credit" not in prompt.lower()
