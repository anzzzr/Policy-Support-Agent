# Policy Support Agent

An LLM-powered customer-support agent that handles refund, cancel, and
address-change requests against a mock SQLite database — while strictly
following a written company policy.

Quality is measured by the **final database state** after scripted
conversations, not by how the chat sounds.

## Stack

- Python 3.11+, SQLite (`sqlite3`), pytest, PyYAML
- One LLM provider (configured for Groq via `GROQ_API_KEY` or `LLM_API_KEY`)
- Default model: `llama-3.3-70b-versatile` (or configure `LLM_MODEL`)
- No frameworks — the agent loop is hand-written

## Quick start

```bash
# Run unit tests (offline, no API key needed)
make test

# Set your Groq API key
export GROQ_API_KEY="gsk_..."

# Interactive chat
make chat

# Run evaluation scenarios
make eval
```

## Project layout

```
policy/policy.md          Written company policy (the agent reads this)
app/db.py                 Schema, seed data, helpers
app/tools.py              The 5 tools + their JSON schemas + policy guard
app/agent.py              The tool-calling loop
app/llm.py                The only file that talks to the LLM provider
scenarios/*.yaml           Test scenarios
eval/run_eval.py          Runs scenarios, scores them, writes eval/RESULTS.md
tests/                    pytest unit tests
Makefile                  Targets: test, chat, eval
```

## Evaluation results

_Placeholder — run `make eval` with an API key to populate._
