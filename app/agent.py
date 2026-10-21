"""
The agent loop — sends messages to the LLM, runs tools, repeats.

~60 lines of real logic.  No framework needed.
"""

import json
import sqlite3
from pathlib import Path

from app.tools import TOOL_SCHEMAS, dispatch

# Stop after this many LLM round-trips per user message.
# Prevents runaway loops if the model keeps calling tools.
MAX_ROUNDS = 8

POLICY_PATH = Path(__file__).resolve().parent.parent / "policy" / "policy.md"

SYSTEM_PROMPT_HEADER = (
    "You are a customer support agent for an online store. You help "
    "customers with refunds, order cancellations, and address changes.\n\n"
    "INSTRUCTIONS:\n"
    "1. Always ask the customer for their order ID and email address FIRST.\n"
    "2. Call lookup_order to verify their identity before taking any action.\n"
    "3. Use the tools provided — never guess or make up information.\n"
    "4. Before issuing a refund, call check_refund_eligibility first.\n"
    "5. Be polite and helpful.\n"
)


# ── System prompt ─────────────────────────────────────────────────────

def build_system_prompt(
    policy_path: Path = POLICY_PATH,
    include_policy: bool = True,
) -> str:
    """Combine role description with policy text at runtime."""
    if not include_policy:
        return SYSTEM_PROMPT_HEADER

    resolved_path = Path(policy_path).resolve()
    if not resolved_path.is_file():
        raise FileNotFoundError(f"Policy file missing at: {resolved_path}")

    raw_text = resolved_path.read_text()
    if not raw_text.strip():
        raise ValueError(f"Policy file is empty at: {resolved_path}")

    return SYSTEM_PROMPT_HEADER + "\nCOMPANY POLICY:\n" + raw_text


# ── Agent loop ────────────────────────────────────────────────────────

def run_turn(
    conn: sqlite3.Connection,
    messages: list[dict],
    trace: list[dict],
    *,
    chat_fn=None,
    metrics: dict | None = None,
    turn: int | None = None,
) -> str:
    """Process one user turn through the tool-calling loop.

    Calls the LLM, runs any requested tools, appends results, and
    repeats until the model returns a plain text reply or MAX_ROUNDS
    is reached.

    Args:
        conn:     Live database connection.
        messages: Conversation history (mutated in place).
        trace:    Tool-call log (mutated in place).
        chat_fn:  Defaults to llm.chat; pass a fake for testing.
        metrics:  Optional dict to accumulate execution stats (e.g. tokens).

    Returns:
        The agent's final text reply for this turn.
    """
    if chat_fn is None:
        from app import llm                # lazy — tests never need the SDK
        chat_fn = llm.chat

    for _ in range(MAX_ROUNDS):
        if metrics is not None:
            metrics["llm_calls"] = metrics.get("llm_calls", 0) + 1
        response = chat_fn(messages, TOOL_SCHEMAS)
        usage = response.pop("_usage", None)
        if usage and metrics is not None:
            metrics["tokens"] = metrics.get("tokens", 0) + usage.get("total_tokens", 0)
            metrics["prompt_tokens"] = metrics.get("prompt_tokens", 0) + usage.get("prompt_tokens", 0)
            metrics["completion_tokens"] = metrics.get("completion_tokens", 0) + usage.get("completion_tokens", 0)
            metrics["llm_retries"] = metrics.get("llm_retries", 0) + usage.get("retries", 0)
            metrics["rate_limit_wait_s"] = metrics.get("rate_limit_wait_s", 0.0) + usage.get("rate_limit_wait_s", 0.0)

        messages.append(response)

        tool_calls = response.get("tool_calls")
        if not tool_calls:
            return response.get("content", "")

        # Execute every tool the model requested in this round.
        for tc in tool_calls:
            func_meta = tc.get("function") or {}
            name = func_meta.get("name", "unknown")
            raw = func_meta.get("arguments", "{}")

            args = None
            result = None

            if isinstance(raw, dict):
                args = raw
            elif isinstance(raw, str):
                try:
                    args = json.loads(raw)
                except Exception as e:
                    args = {"raw": raw}
                    result = {
                        "ok": False,
                        "success": False,
                        "error": f"Malformed JSON arguments for tool '{name}': {e}",
                    }
            else:
                args = {"raw": str(raw)}
                result = {
                    "ok": False,
                    "success": False,
                    "error": f"Invalid arguments type for tool '{name}'. Expected JSON string.",
                }

            if result is None:
                if not isinstance(args, dict):
                    result = {
                        "ok": False,
                        "success": False,
                        "error": f"Tool arguments for '{name}' must decode to a JSON object.",
                    }
                else:
                    result = dispatch(conn, name, args)

            trace_entry = {
                "tool": name,
                "args": args,
                "result": result,
                "blocked": (
                    result.get("ok") is False
                    and "Policy violation" in result.get("error", "")
                ),
            }
            if turn is not None:
                trace_entry["turn"] = turn
            trace.append(trace_entry)

            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id", "call_unknown"),
                "content": json.dumps(result),
            })

    # Safety net — should rarely fire with a well-prompted model.
    return (
        "I've reached the maximum number of steps for this request. "
        "Let me escalate this to a human supervisor."
    )


# ── CLI entrypoint (`make chat` / `python -m app.agent`) ─────────────

def main():
    from app.db import get_db, seed

    # Fresh DB every session so the chat always starts from known state.
    conn = get_db(":memory:")
    seed(conn)

    messages = [{"role": "system", "content": build_system_prompt()}]
    trace: list[dict] = []

    print("Policy Support Agent  (type 'quit' to exit)")
    print("-" * 46)

    while True:
        try:
            user_input = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break

        if not user_input or user_input.lower() in ("quit", "exit"):
            print("Goodbye!")
            break

        messages.append({"role": "user", "content": user_input})
        reply = run_turn(conn, messages, trace)
        print(f"\nAgent: {reply}")


if __name__ == "__main__":
    main()
