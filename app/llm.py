"""
LLM provider abstraction — the ONLY file that talks to the API.

Uses the OpenAI SDK (works with any OpenAI-compatible endpoint).
Swap providers by editing this file alone.

Env vars:
    LLM_API_KEY  — API key (required)
    LLM_MODEL    — model name (default: gpt-4o-mini)
"""

import json
import os

# Lazy-safe import so the rest of the app can be tested without the SDK.
try:
    import openai
except ImportError:
    openai = None


def chat(messages: list[dict], tools: list[dict]) -> dict:
    """Send a chat-completion request and return the assistant message.

    Returns a plain dict (not an SDK object) so nothing outside this file
    depends on the openai package.

    Tool-call arguments are kept as JSON *strings* — the agent loop parses
    them.  This avoids an extra serialize round-trip when the dict gets
    appended back into the messages list for the next API call.
    """
    if openai is None:
        raise RuntimeError(
            "The 'openai' package is required.  Install: pip install openai"
        )

    api_key = os.environ.get("LLM_API_KEY")
    if not api_key:
        raise RuntimeError("Set the LLM_API_KEY environment variable.")

    model = os.environ.get("LLM_MODEL", "gpt-4o-mini")
    client = openai.OpenAI(api_key=api_key)

    kwargs: dict = {
        "model": model,
        "messages": messages,
        "temperature": 0,
    }
    if tools:
        kwargs["tools"] = tools

    resp = client.chat.completions.create(**kwargs)
    msg = resp.choices[0].message

    # ── Normalize to a plain dict ──
    result: dict = {"role": "assistant", "content": msg.content}

    if msg.tool_calls:
        result["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,   # JSON string
                },
            }
            for tc in msg.tool_calls
        ]

    return result
