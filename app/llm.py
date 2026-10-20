"""
LLM provider abstraction — the ONLY file that talks to the API.

Configured for Groq's high-speed OpenAI-compatible endpoint.
Swap providers by editing this file alone.

Env vars:
    LLM_API_KEY / GROQ_API_KEY — API key (required)
    LLM_MODEL                  — model name (default: llama-3.3-70b-versatile)
    LLM_BASE_URL               — API base URL (default: https://api.groq.com/openai/v1)
"""

import json
import os

# Lazy-safe import so the rest of the app can be tested without the SDK.
try:
    import openai
except ImportError:
    openai = None

DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "openai/gpt-oss-120b"


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

    # Prioritize GROQ_API_KEY, then LLM_API_KEY
    api_key = os.environ.get("GROQ_API_KEY") or os.environ.get("LLM_API_KEY")
    if not api_key:
        raise RuntimeError("Set GROQ_API_KEY (or LLM_API_KEY) environment variable.")

    base_url = os.environ.get("LLM_BASE_URL", DEFAULT_BASE_URL)
    model = os.environ.get("LLM_MODEL")

    # If hitting Groq but model is set to an OpenAI model (like gpt-6-luna or gpt-4o-mini), use Groq's model
    if not model or ("api.groq.com" in base_url and (model.startswith("gpt-") or model.startswith("o1-") or model.startswith("o3-"))):
        model = DEFAULT_MODEL

    client = openai.OpenAI(api_key=api_key, base_url=base_url)

    kwargs: dict = {
        "model": model,
        "messages": messages,
        "temperature": 0,
    }
    if tools:
        kwargs["tools"] = tools

    # Support reasoning models if passed
    effort = os.environ.get("LLM_REASONING_EFFORT")
    if effort:
        kwargs["reasoning_effort"] = effort
    elif any(k in model.lower() for k in ["gpt-6", "luna", "o1", "o3"]):
        kwargs["reasoning_effort"] = "none"

    try:
        resp = client.chat.completions.create(**kwargs)
    except openai.RateLimitError:
        print(" [Rate-limit: waiting 15s for Groq TPM bucket...] ", end="", flush=True)
        import time
        time.sleep(15)
        resp = client.chat.completions.create(**kwargs)
    except openai.BadRequestError as e:
        err_msg = str(e).lower()
        retry_needed = False
        if "reasoning_effort" in err_msg and kwargs.get("reasoning_effort") != "none":
            kwargs["reasoning_effort"] = "none"
            retry_needed = True
        if "temperature" in err_msg and "temperature" in kwargs:
            kwargs.pop("temperature", None)
            retry_needed = True

        if retry_needed:
            try:
                resp = client.chat.completions.create(**kwargs)
            except openai.BadRequestError as e2:
                err_msg2 = str(e2).lower()
                if "temperature" in err_msg2 and "temperature" in kwargs:
                    kwargs.pop("temperature", None)
                    resp = client.chat.completions.create(**kwargs)
                else:
                    raise
        else:
            raise
    msg = resp.choices[0].message

    # ── Normalize to a plain dict ──
    result: dict = {"role": "assistant", "content": msg.content}

    if hasattr(resp, "usage") and resp.usage:
        result["_usage"] = {
            "prompt_tokens": getattr(resp.usage, "prompt_tokens", 0),
            "completion_tokens": getattr(resp.usage, "completion_tokens", 0),
            "total_tokens": getattr(resp.usage, "total_tokens", 0),
        }

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
