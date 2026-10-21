# Policy Support Agent

A deterministic, test-driven customer support LLM agent built without heavyweight frameworks that handles refunds, order cancellations, and shipping address modifications against a mock SQLite backend while strictly enforcing written company policy. Instead of evaluating conversation style or subjective chat fluency, system quality is measured objectively by the **final transactional state of the database** after multi-turn scripted conversations, proving defense-in-depth against prompt injections, social pressure, and out-of-policy requests.

---

## Architecture

```text
User Turn
   │
   ▼
[app/agent.py]  Hand-crafted 60-line agent loop with MAX_ROUNDS safeguard
   │
   ▼
[app/tools.py]  Tool router with JSON schemas and policy validation
   │
   ▼
[Policy Guard]  Deterministic Python code gate enforcing business rules
   │
   ▼
[app/db.py]     ACID-compliant SQLite database (orders, customers, refunds, escalations)
```

---

## Company Policy Summary

For the full policy, see [policy/policy.md](policy/policy.md).

### Architectural Layers & Guard Behavior

The codebase implements two distinct defense-in-depth validation layers:

1. **Always-On Validation (Active under BOTH `GUARD=on` and `GUARD=off`):**
   - **Type & Number Integrity:** Input types, non-null, NaN/Infinity rejection, positive integers for IDs.
   - **SQL Injection Prevention:** Strictly parameterized SQL queries and non-integer order ID rejections.
   - **Data Sanitization:** Rejection of control characters, newlines (`\n`, `\r`), and addresses exceeding 300 characters.
   - **Existence Checks:** Clean error reporting (`"Order not found."`) when an order ID does not exist in the database, without modifying database state.

2. **Policy Guard Rules 1–7 (Active ONLY when `GUARD=on`, Disabled when `GUARD=off`):**
   - **Rule 1 (Identity Verification):** Mutating tools and eligibility checks require session verification via `lookup_order`.
   - **Rule 2 (Delivery & Window):** Refunds require order status `delivered` and within 30 days of delivery.
   - **Rule 3 (Final Sale):** Items flagged `is_final_sale=1` cannot be refunded.
   - **Rule 4 (Amount & Threshold):** Refunds must equal full order total; amounts > ₹5,000 must escalate to human.
   - **Rule 5 (Single Refund):** Exactly one refund per order ID (blocks double refunds).
   - **Rule 6 (Cancellation Window):** Cancellations allowed only while status is `processing` (blocks cancellation of shipped, delivered, or already cancelled/refunded orders).
   - **Rule 7 (Address Updates):** Address changes allowed only while status is `processing`.

> [!NOTE]
> When `GUARD=off` is set (used exclusively for ablation benchmarks in Variant C), Layer 2 policy enforcement gates are disabled to measure model compliance from prompt instructions alone, while Layer 1 always-on integrity checks remain active.

---

## How to Run

### 1. Setup & Unit Tests
Clone the repository and run all 83 offline tests (completes in < 0.1s without network calls or API keys):
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.lock

# Run all unit tests
make test
```

### 2. Interactive CLI Chat
Interact with the agent directly in terminal:
```bash
export GROQ_API_KEY="gsk_..."
make chat
```

### 3. Evaluation Harness
Execute the 30 automated test scenarios:
```bash
export GROQ_API_KEY="gsk_..."
# Run full evaluation
make eval

# Or run the primary configuration directly
python3 eval/run_eval.py --variant B --runs 1
```

---

## Evaluation Benchmark

> [!NOTE]
> Evaluation on policy v2 is in progress. The initial legacy run is archived in `eval/published/legacy/`.

---

## Limitations

- **Fixed User Scripts:** Evaluation uses static scripted multi-turn prompts rather than an interactive adversarial LLM-as-a-judge generating dynamic customer turns.
- **Single Model Testing:** Evaluation on policy v2 is in progress; the initial legacy run is archived in `eval/published/legacy/`. Smaller models may exhibit higher prompt injection susceptibility without code guards.
- **Small Dataset:** Database seed comprises 12 customers and 25 orders; large enterprise databases would require index tuning and pagination.
- **Mock Backend:** Relies on an in-memory SQLite database rather than external logistics APIs, payment gateway webhooks (Stripe/Razorpay), or distributed message queues.

---

## What I Would Do Next

1. **LLM-Driven Dynamic User Simulation:** Replace static YAML turn scripts with an adversarial user simulator model instructed to probe boundaries, negotiate, and attempt jailbreaks dynamically.
2. **Asynchronous Human-in-the-Loop Review Queue:** Connect `escalate_to_human` to a real ticketing webhook (e.g., Zendesk or Slack) where human operators can approve exceptions and resume the agent's paused session.
3. **Cross-Model Benchmark Suite:** Run the automated evaluation suite against GPT-4o, Claude 3.5 Sonnet, and Llama 3.3 70B to evaluate how parameter size impacts reliance on tool-level guards.
