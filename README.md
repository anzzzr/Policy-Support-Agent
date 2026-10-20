# Policy Support Agent

An LLM-powered customer-support agent that handles refund, cancel, and
address-change requests against a mock SQLite database — while strictly
following a written company policy.

Quality is measured by the **final database state** after scripted
conversations, not by how the chat sounds.

## Stack

- Python 3.11+, SQLite (`sqlite3`), pytest, PyYAML
- One LLM provider (set `LLM_API_KEY` and `LLM_MODEL` env vars)
- No frameworks — the agent loop is hand-written

## Quick start

```bash
# Run unit tests
make test

# Interactive chat (requires LLM_API_KEY)
make chat

# Run evaluation scenarios (requires LLM_API_KEY)
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
