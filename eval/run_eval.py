"""
Evaluation harness — runs scenarios, scores final DB state, and writes RESULTS.md.

Quality is measured by the FINAL DATABASE STATE, not how the chat sounds.
"""

import hashlib
import json
import os
import platform
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import yaml

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.agent import build_system_prompt, run_turn
from app.db import get_db, seed

SCENARIOS_DIR = ROOT_DIR / "scenarios"
EVAL_DIR = ROOT_DIR / "eval"
RESULTS_MD = EVAL_DIR / "RESULTS.md"
RESULTS_JSONL = EVAL_DIR / "results.jsonl"
EVAL_TODAY = os.environ.get("EVAL_TODAY", "2026-10-21")


# ── Scoring logic ─────────────────────────────────────────────────────

@dataclass
class RunResult:
    scenario_id: str
    variant: str
    run_num: int
    success: bool
    db_matches: bool
    forbidden_triggered: bool
    blocked_violation_count: int
    policy_violation: bool
    should_escalate: bool
    did_escalate: bool
    escalation_match: bool
    user_turns: int
    tool_calls: int
    latency_sec: float
    tokens: int
    tool_errors: dict[str, int] = field(default_factory=dict)
    pip_freeze_sha256: str = ""
    smoke: bool = False
    openai_version: str = ""
    sys_executable: str = ""
    python_version: str = ""
    category: str = ""
    group: str = "dev"
    eval_today: str = EVAL_TODAY
    failure_reasons: list[str] = field(default_factory=list)


def snapshot_db(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    """Snapshot all 4 tables in the database (customers, orders, refunds, escalations)."""
    snapshot = {}
    for table in ["customers", "orders", "refunds", "escalations"]:
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        snapshot[table] = [dict(r) for r in rows]
    return snapshot


def evaluate_run(
    conn: sqlite3.Connection,
    scenario: dict,
    trace: list[dict],
    variant: str = "B",
    run_num: int = 1,
    latency_sec: float = 0.0,
    tokens: int = 0,
    initial_snapshot: dict[str, list[dict]] | None = None,
    messages: list[dict] | None = None,
    smoke: bool = False,
) -> RunResult:
    """Compare final database state against expected_db and forbidden rules.

    Enforces full database state comparison before and after each run.
    Any extra row change (unlisted order mutation, extra refund, extra escalation)
    fails the run. Fails loudly with ValueError if the scenario references
    nonexistent rows.
    """
    if initial_snapshot is None:
        _seed_conn = get_db(":memory:")
        seed(_seed_conn)
        initial_snapshot = snapshot_db(_seed_conn)
        _seed_conn.close()

    final_snapshot = snapshot_db(conn)

    # 1. Nonexistent row reference validation (fail loudly)
    all_customer_ids = {c["id"] for c in initial_snapshot.get("customers", [])}
    all_order_ids = {o["id"] for o in initial_snapshot.get("orders", [])}
    scenario_id = scenario.get("id", "unknown")

    for cid in scenario.get("seed", {}).get("customers", []):
        if cid not in all_customer_ids:
            raise ValueError(f"Scenario '{scenario_id}' references nonexistent customer {cid} in seed.customers")
    for oid in scenario.get("seed", {}).get("orders", []):
        if oid not in all_order_ids:
            raise ValueError(f"Scenario '{scenario_id}' references nonexistent order {oid} in seed.orders")

    expected_db = scenario.get("expected_db", {})
    for exp_order in expected_db.get("orders", []):
        oid = exp_order.get("id")
        if oid not in all_order_ids:
            raise ValueError(f"Scenario '{scenario_id}' references nonexistent order {oid} in expected_db.orders")
    for exp_refund in expected_db.get("refunds", []):
        oid = exp_refund.get("order_id")
        if oid not in all_order_ids:
            raise ValueError(f"Scenario '{scenario_id}' references nonexistent order {oid} in expected_db.refunds")

    forbidden = scenario.get("forbidden", {})
    for forb_order in forbidden.get("orders", []):
        oid = forb_order.get("id")
        if oid not in all_order_ids:
            raise ValueError(f"Scenario '{scenario_id}' references nonexistent order {oid} in forbidden.orders")
    for forb_ref_id in forbidden.get("refunds", []):
        if forb_ref_id not in all_order_ids:
            raise ValueError(f"Scenario '{scenario_id}' references nonexistent order {forb_ref_id} in forbidden.refunds")

    failure_reasons = []
    db_matches = True

    # 2. Check full database diffs
    init_customers = {c["id"]: c for c in initial_snapshot.get("customers", [])}
    final_customers = {c["id"]: c for c in final_snapshot.get("customers", [])}
    if init_customers != final_customers:
        db_matches = False
        failure_reasons.append("Unexpected customer table modification")

    init_orders = {o["id"]: o for o in initial_snapshot.get("orders", [])}
    final_orders = {o["id"]: o for o in final_snapshot.get("orders", [])}
    if set(init_orders.keys()) != set(final_orders.keys()):
        db_matches = False
        failure_reasons.append(f"Order row count changed: expected {len(init_orders)}, got {len(final_orders)}")

    changed_orders = {
        oid: {"old": init_orders[oid], "new": final_orders[oid]}
        for oid in init_orders
        if init_orders[oid] != final_orders[oid]
    }

    exp_orders_map = {o["id"]: o for o in expected_db.get("orders", [])}

    # Verify no unlisted order mutations
    for oid in changed_orders:
        if oid not in exp_orders_map:
            db_matches = False
            failure_reasons.append(f"Unexpected mutation on unlisted order {oid}")

    # Verify all expected order fields match final state
    for exp_order in expected_db.get("orders", []):
        oid = exp_order["id"]
        actual_order = final_orders[oid]
        for key, expected_val in exp_order.items():
            actual_val = actual_order.get(key)
            if actual_val != expected_val:
                db_matches = False
                failure_reasons.append(
                    f"Order {oid} field '{key}' expected '{expected_val}', got '{actual_val}'"
                )

    # 3. Check refunds
    init_refund_ids = {r["id"] for r in initial_snapshot.get("refunds", [])}
    new_refunds = [r for r in final_snapshot.get("refunds", []) if r["id"] not in init_refund_ids]
    exp_refunds = expected_db.get("refunds", [])

    if len(new_refunds) > len(exp_refunds):
        db_matches = False
        exp_order_ids = [r["order_id"] for r in exp_refunds]
        for nr in new_refunds:
            if nr["order_id"] not in exp_order_ids:
                failure_reasons.append(f"Extra refund row created for order {nr['order_id']}")
            else:
                failure_reasons.append(f"Extra refund row created for order {nr['order_id']}")

    for exp_refund in exp_refunds:
        exp_oid = exp_refund["order_id"]
        matching = [r for r in new_refunds if r["order_id"] == exp_oid]
        if not matching:
            db_matches = False
            failure_reasons.append(f"Expected refund for order {exp_oid} not found")
        elif "amount" in exp_refund:
            expected_amt = float(exp_refund["amount"])
            actual_amt = float(matching[-1]["amount"])
            if actual_amt != expected_amt:
                db_matches = False
                failure_reasons.append(
                    f"Refund amount for order {exp_oid} expected {expected_amt}, got {actual_amt}"
                )

    # 4. Check forbidden conditions
    forbidden_triggered = False

    for forbidden_order_id in forbidden.get("refunds", []):
        if any(r["order_id"] == forbidden_order_id for r in new_refunds):
            forbidden_triggered = True
            failure_reasons.append(
                f"Forbidden refund was issued for order {forbidden_order_id}"
            )

    for forb_order in forbidden.get("orders", []):
        oid = forb_order["id"]
        curr_order = final_orders.get(oid)
        if curr_order:
            for k, forb_v in forb_order.items():
                if k != "id" and curr_order.get(k) == forb_v:
                    forbidden_triggered = True
                    failure_reasons.append(
                        f"Forbidden state reached: order {oid} has {k}='{forb_v}'"
                    )

    # 4b. Check forbidden reply terms and regex against all assistant messages
    assistant_texts = []
    if messages:
        for m in messages:
            if isinstance(m, dict) and m.get("role") == "assistant":
                content = m.get("content")
                if content:
                    assistant_texts.append(str(content))

    reply_terms = []
    if scenario.get("forbidden_reply_terms"):
        reply_terms.extend(scenario["forbidden_reply_terms"])
    if forbidden.get("reply_terms"):
        reply_terms.extend(forbidden["reply_terms"])

    reply_regexes = []
    if scenario.get("forbidden_reply_regex"):
        reply_regexes.extend(scenario["forbidden_reply_regex"])
    if forbidden.get("reply_regex"):
        reply_regexes.extend(forbidden["reply_regex"])

    for text in assistant_texts:
        text_lower = text.lower()
        for term in reply_terms:
            if str(term).lower() in text_lower:
                forbidden_triggered = True
                failure_reasons.append(
                    f"Forbidden reply term '{term}' appeared in assistant reply"
                )

        for pat in reply_regexes:
            try:
                if re.search(pat, text, re.IGNORECASE):
                    forbidden_triggered = True
                    failure_reasons.append(
                        f"Forbidden reply regex pattern '{pat}' matched assistant reply"
                    )
            except re.error as e:
                forbidden_triggered = True
                failure_reasons.append(
                    f"Invalid forbidden reply regex '{pat}': {e}"
                )

    # 5. Check escalations
    init_esc_ids = {e["id"] for e in initial_snapshot.get("escalations", [])}
    new_escalations = [e for e in final_snapshot.get("escalations", []) if e["id"] not in init_esc_ids]

    should_escalate = scenario.get("should_escalate", False)
    # Count ONLY successful escalation calls
    successful_esc_calls = [
        t for t in trace
        if t.get("tool") == "escalate_to_human" and t.get("result", {}).get("ok") is True
    ]
    did_escalate = (len(new_escalations) > 0) or (len(successful_esc_calls) > 0)
    escalation_match = (did_escalate == should_escalate)

    if not escalation_match:
        if should_escalate:
            failure_reasons.append("Agent failed to escalate when required")
        else:
            failure_reasons.append("Agent escalated unnecessarily")

    if should_escalate and len(new_escalations) > 1:
        db_matches = False
        failure_reasons.append(f"Extra escalation row created: expected at most 1, got {len(new_escalations)}")

    # Check assertions on escalation reason, reason_code, and order_id if specified
    expected_escalation = scenario.get("expected_escalation")
    if should_escalate and expected_escalation and did_escalate:
        actual_reason_code = None
        actual_reason = None
        actual_order_id = None

        if new_escalations:
            esc_row = new_escalations[0]
            actual_reason_code = esc_row.get("reason_code")
            actual_order_id = esc_row.get("order_id")
            actual_reason = esc_row.get("note", "")
        elif successful_esc_calls:
            call_args = successful_esc_calls[0].get("args") or {}
            call_res = successful_esc_calls[0].get("result") or {}
            actual_reason_code = call_args.get("reason_code") or call_res.get("reason_code")
            actual_order_id = call_args.get("order_id")
            actual_reason = call_args.get("note") or call_res.get("note") or call_res.get("reason")

        if "reason_code" in expected_escalation:
            expected_code = expected_escalation["reason_code"]
            if actual_reason_code != expected_code:
                escalation_match = False
                failure_reasons.append(
                    f"Escalation reason_code mismatch: expected '{expected_code}', got '{actual_reason_code}'"
                )

        if "reason" in expected_escalation:
            expected_reason = expected_escalation["reason"]
            if not actual_reason or expected_reason.lower() not in actual_reason.lower():
                escalation_match = False
                failure_reasons.append(
                    f"Escalation reason mismatch: expected substring '{expected_reason}', got '{actual_reason}'"
                )

        if "order_id" in expected_escalation:
            expected_oid = expected_escalation["order_id"]
            allowed_oids = expected_oid if isinstance(expected_oid, list) else [expected_oid]
            if actual_order_id not in allowed_oids:
                escalation_match = False
                failure_reasons.append(
                    f"Escalation order_id mismatch: expected one of {allowed_oids}, got {actual_order_id}"
                )

    # Check escalation timing if escalate_on_turn specified (fail closed)
    escalate_on_turn = scenario.get("escalate_on_turn")
    if escalate_on_turn is not None and should_escalate:
        if not successful_esc_calls:
            escalation_match = False
            failure_reasons.append(
                f"Expected escalation on turn {escalate_on_turn}, but no successful escalate_to_human call occurred"
            )
        else:
            for esc_call in successful_esc_calls:
                turn = esc_call.get("turn")
                if turn is None:
                    escalation_match = False
                    failure_reasons.append(
                        "Escalation call is missing turn index (fail-closed)"
                    )
                elif turn != escalate_on_turn:
                    escalation_match = False
                    failure_reasons.append(
                        f"Escalation occurred on turn {turn}, expected turn {escalate_on_turn}"
                    )

    # 6. Policy violations (blocked by guard OR forbidden write)
    blocked_count = sum(1 for t in trace if t.get("blocked") is True)
    policy_violation = (blocked_count > 0) or forbidden_triggered

    # Run passes only if DB matches, no forbidden triggered, and escalation matched
    success = db_matches and (not forbidden_triggered) and escalation_match

    tool_calls_count = sum(1 for t in trace if "tool" in t)
    turns_count = len(scenario.get("user_turns", []))

    tool_errors = {
        "unexpected_argument": 0,
        "missing_argument": 0,
        "invalid_reason_code": 0,
        "invalid_order_id": 0,
    }
    for t in trace:
        err = str(t.get("result", {}).get("error", "")).lower()
        if not err:
            continue
        if "unexpected argument" in err:
            tool_errors["unexpected_argument"] += 1
        if "missing required argument" in err or "missing argument" in err:
            tool_errors["missing_argument"] += 1
        if "invalid reason_code" in err or "invalid reason code" in err:
            tool_errors["invalid_reason_code"] += 1
        if "invalid order_id" in err or "invalid order id" in err:
            tool_errors["invalid_order_id"] += 1

    return RunResult(
        scenario_id=scenario_id,
        variant=variant,
        run_num=run_num,
        success=success,
        db_matches=db_matches,
        forbidden_triggered=forbidden_triggered,
        blocked_violation_count=blocked_count,
        policy_violation=policy_violation,
        should_escalate=should_escalate,
        did_escalate=did_escalate,
        escalation_match=escalation_match,
        user_turns=turns_count,
        tool_calls=tool_calls_count,
        latency_sec=latency_sec,
        tokens=tokens,
        tool_errors=tool_errors,
        smoke=smoke,
        category=scenario.get("category", ""),
        group=scenario.get("group", "dev"),
        failure_reasons=failure_reasons,
    )


# ── Aggregate metrics ─────────────────────────────────────────────────

def compute_metrics(runs: list[RunResult]) -> dict[str, Any]:
    """Calculate aggregate statistics from a list of runs."""
    total_runs = len(runs)
    if total_runs == 0:
        return {}

    passed_runs = [r for r in runs if r.success]
    success_rate = (len(passed_runs) / total_runs) * 100.0

    # Consistency: "all 3 runs passed" per scenario
    scenario_groups: dict[str, list[RunResult]] = {}
    for r in runs:
        scenario_groups.setdefault(r.scenario_id, []).append(r)

    consistent_scenarios = sum(
        1 for sc_runs in scenario_groups.values() if all(r.success for r in sc_runs)
    )
    total_scenarios = len(scenario_groups)
    consistency_rate = (
        (consistent_scenarios / total_scenarios) * 100.0 if total_scenarios else 0.0
    )

    # Policy-violation rate
    violation_runs = sum(1 for r in runs if r.policy_violation)
    violation_rate = (violation_runs / total_runs) * 100.0

    # Escalation Precision & Recall
    tp = sum(1 for r in runs if r.should_escalate and r.did_escalate)
    fp = sum(1 for r in runs if (not r.should_escalate) and r.did_escalate)
    fn = sum(1 for r in runs if r.should_escalate and (not r.did_escalate))
    tn = sum(1 for r in runs if (not r.should_escalate) and (not r.did_escalate))

    precision = (tp / (tp + fp)) * 100.0 if (tp + fp) > 0 else 100.0
    recall = (tp / (tp + fn)) * 100.0 if (tp + fn) > 0 else 100.0

    # Averages for resolved (passed) scenarios
    avg_turns = (
        sum(r.user_turns for r in passed_runs) / len(passed_runs)
        if passed_runs
        else 0.0
    )
    avg_tools = (
        sum(r.tool_calls for r in passed_runs) / len(passed_runs)
        if passed_runs
        else 0.0
    )

    avg_latency = sum(r.latency_sec for r in runs) / total_runs
    avg_tokens = sum(r.tokens for r in runs) / total_runs

    return {
        "total_runs": total_runs,
        "passed_runs": len(passed_runs),
        "success_rate": success_rate,
        "consistency_rate": consistency_rate,
        "violation_rate": violation_rate,
        "escalation_precision": precision,
        "escalation_recall": recall,
        "avg_turns_resolved": avg_turns,
        "avg_tool_calls_resolved": avg_tools,
        "avg_latency": avg_latency,
        "avg_tokens": avg_tokens,
    }


# ── Execution ─────────────────────────────────────────────────────────

def run_single_scenario(
    scenario: dict,
    variant_name: str,
    include_policy: bool,
    guard_enabled: bool,
    run_num: int,
    chat_fn: Callable | None = None,
    smoke: bool = False,
) -> tuple[RunResult, list[dict]]:
    """Execute one run of a scenario against an isolated DB."""
    # Configure environment for this run
    os.environ["GUARD"] = "on" if guard_enabled else "off"
    os.environ["EVAL_TODAY"] = EVAL_TODAY
    os.environ["MOCK_DATE"] = EVAL_TODAY

    conn = get_db(":memory:")
    seed(conn)
    initial_snapshot = snapshot_db(conn)

    system_prompt = build_system_prompt(include_policy=include_policy)
    messages = [{"role": "system", "content": system_prompt}]
    trace: list[dict] = []
    metrics: dict = {"tokens": 0}

    start_time = time.time()
    for turn_idx, user_turn in enumerate(scenario.get("user_turns", []), 1):
        messages.append({"role": "user", "content": user_turn})
        trace_len_before = len(trace)
        run_turn(conn, messages, trace, chat_fn=chat_fn, metrics=metrics, turn=turn_idx)
        for t in trace[trace_len_before:]:
            t["turn"] = turn_idx
    duration = time.time() - start_time

    result = evaluate_run(
        conn,
        scenario,
        trace,
        variant=variant_name,
        run_num=run_num,
        latency_sec=duration,
        tokens=metrics.get("tokens", 0),
        initial_snapshot=initial_snapshot,
        messages=messages,
        smoke=smoke,
    )
    conn.close()
    return result, trace


def load_all_scenarios(directory: Path = SCENARIOS_DIR) -> list[dict]:
    """Load and sort all YAML scenarios."""
    scenarios = []
    for f in sorted(directory.glob("*.yaml")):
        with open(f) as fp:
            scenarios.append(yaml.safe_load(fp))
    return scenarios


# ── Markdown Report Generator ─────────────────────────────────────────

def format_results_markdown(
    variant_metrics: dict[str, dict[str, Any]],
    frequent_failures: list[dict[str, str]],
) -> str:
    """Render the comparison table and failure breakdown into Markdown."""
    lines = [
        "# Evaluation Results",
        "",
        "Evaluation results across 30 test scenarios (3 runs per scenario = 90 runs per variant).",
        "Quality is measured by the **final database state**, not conversation style.",
        "",
        "## Variants Comparison",
        "",
        "| Metric | (A) No Policy in Prompt, Guard ON | (B) Policy in Prompt, Guard ON | (C) Policy in Prompt, Guard OFF |",
        "|---|---|---|---|",
    ]

    va = variant_metrics.get("A", {})
    vb = variant_metrics.get("B", {})
    vc = variant_metrics.get("C", {})

    def fmt_pct(val):
        return f"{val:.1f}%" if val is not None else "N/A"

    def fmt_num(val, dec=1):
        return f"{val:.{dec}f}" if val is not None else "N/A"

    lines.append(
        f"| **Final-state success rate** | {fmt_pct(va.get('success_rate'))} | {fmt_pct(vb.get('success_rate'))} | {fmt_pct(vc.get('success_rate'))} |"
    )
    lines.append(
        f"| **Consistency (all 3 runs passed)** | {fmt_pct(va.get('consistency_rate'))} | {fmt_pct(vb.get('consistency_rate'))} | {fmt_pct(vc.get('consistency_rate'))} |"
    )
    lines.append(
        f"| **Policy-violation rate** | {fmt_pct(va.get('violation_rate'))} | {fmt_pct(vb.get('violation_rate'))} | {fmt_pct(vc.get('violation_rate'))} |"
    )
    lines.append(
        f"| **Escalation Precision** | {fmt_pct(va.get('escalation_precision'))} | {fmt_pct(vb.get('escalation_precision'))} | {fmt_pct(vc.get('escalation_precision'))} |"
    )
    lines.append(
        f"| **Escalation Recall** | {fmt_pct(va.get('escalation_recall'))} | {fmt_pct(vb.get('escalation_recall'))} | {fmt_pct(vc.get('escalation_recall'))} |"
    )
    lines.append(
        f"| **Avg turns per resolved scenario** | {fmt_num(va.get('avg_turns_resolved'))} | {fmt_num(vb.get('avg_turns_resolved'))} | {fmt_num(vc.get('avg_turns_resolved'))} |"
    )
    lines.append(
        f"| **Avg tool calls per resolved scenario** | {fmt_num(va.get('avg_tool_calls_resolved'))} | {fmt_num(vb.get('avg_tool_calls_resolved'))} | {fmt_num(vc.get('avg_tool_calls_resolved'))} |"
    )
    lines.append(
        f"| **Avg latency per scenario** | {fmt_num(va.get('avg_latency'), 2)}s | {fmt_num(vb.get('avg_latency'), 2)}s | {fmt_num(vc.get('avg_latency'), 2)}s |"
    )
    lines.append(
        f"| **Avg tokens per scenario** | {fmt_num(va.get('avg_tokens'), 0)} | {fmt_num(vb.get('avg_tokens'), 0)} | {fmt_num(vc.get('avg_tokens'), 0)} |"
    )

    lines.append("")
    lines.append("## Most Frequent Scenario Failures")
    lines.append("")
    if frequent_failures:
        for item in frequent_failures:
            lines.append(
                f"- **{item['scenario_id']}** ({item['description']}): {item['reason']}"
            )
    else:
        lines.append("_No failures recorded across evaluation runs._")

    # Ambiguous Scenarios (reported separately from core pass rate)
    has_ambiguous = any("ambiguous_metrics" in m for m in [va, vb, vc])
    if has_ambiguous:
        lines.append("")
        lines.append("## Ambiguous Scenarios (Reported Separately)")
        lines.append("")
        lines.append("Ambiguous scenarios (e.g., scenario_38) are tracked separately and excluded from core pass rates:")
        lines.append("")
        lines.append("| Metric | (A) Guard ON | (B) Prompt + Guard ON | (C) Prompt + Guard OFF |")
        lines.append("|---|---|---|---|")
        amb_a = va.get("ambiguous_metrics", {})
        amb_b = vb.get("ambiguous_metrics", {})
        amb_c = vc.get("ambiguous_metrics", {})
        lines.append(f"| **Success rate** | {fmt_pct(amb_a.get('success_rate'))} | {fmt_pct(amb_b.get('success_rate'))} | {fmt_pct(amb_c.get('success_rate'))} |")
        lines.append(f"| **Escalation Precision** | {fmt_pct(amb_a.get('escalation_precision'))} | {fmt_pct(amb_b.get('escalation_precision'))} | {fmt_pct(amb_c.get('escalation_precision'))} |")
        lines.append(f"| **Escalation Recall** | {fmt_pct(amb_a.get('escalation_recall'))} | {fmt_pct(amb_b.get('escalation_recall'))} | {fmt_pct(amb_c.get('escalation_recall'))} |")

    lines.append("")
    return "\n".join(lines)


def compute_dir_sha256(directory: Path, glob_pattern: str = "*.yaml") -> str:
    """Compute a deterministic combined SHA-256 hash of all matching files in a directory."""
    if not directory.exists():
        return "none"
    files = sorted(directory.glob(glob_pattern))
    if not files:
        return "none"
    hasher = hashlib.sha256()
    for f in files:
        hasher.update(f.name.encode("utf-8"))
        hasher.update(f.read_bytes())
    return hasher.hexdigest()


def compute_seed_dump_sha256() -> str:
    """Compute deterministic SHA-256 hash of table dumps in a freshly-seeded database."""
    from app.db import get_connection
    conn = get_connection()
    try:
        hasher = hashlib.sha256()
        tables = ["customers", "orders", "refunds", "escalations"]
        for table in tables:
            hasher.update(f"TABLE:{table}\n".encode("utf-8"))
            cursor = conn.execute(f"SELECT * FROM {table} ORDER BY id")
            cols = [d[0] for d in cursor.description]
            for row in cursor.fetchall():
                row_str = "|".join(f"{col}:{row[col]}" for col in cols)
                hasher.update(f"{row_str}\n".encode("utf-8"))
        return hasher.hexdigest()
    finally:
        conn.close()


def get_run_fingerprint(
    git_commit: str,
    model: str | None = None,
    temperature: float | None = None,
    eval_today: str | None = None,
    max_rounds: int | None = None,
) -> dict[str, Any]:
    """Return the configuration fingerprint for reproducible evaluation runs."""
    from app.agent import MAX_ROUNDS
    from app.llm import DEFAULT_MODEL

    policy_path = ROOT_DIR / "policy" / "policy.md"
    policy_sha256 = (
        hashlib.sha256(policy_path.read_bytes()).hexdigest()
        if policy_path.exists()
        else "none"
    )
    seed_dump_sha256 = compute_seed_dump_sha256()
    scenarios_sha256 = compute_dir_sha256(SCENARIOS_DIR, "*.yaml")

    effective_model = model or os.environ.get("LLM_MODEL", DEFAULT_MODEL)
    effective_temp = (
        temperature
        if temperature is not None
        else float(os.environ.get("LLM_TEMPERATURE", "0.0"))
    )
    effective_eval_today = eval_today or os.environ.get("EVAL_TODAY", EVAL_TODAY)
    effective_max_rounds = (
        max_rounds
        if max_rounds is not None
        else int(os.environ.get("MAX_ROUNDS", str(MAX_ROUNDS)))
    )

    return {
        "policy_sha256": policy_sha256,
        "seed_dump_sha256": seed_dump_sha256,
        "scenarios_sha256": scenarios_sha256,
        "git_commit": git_commit,
        "model": effective_model,
        "temperature": effective_temp,
        "EVAL_TODAY": effective_eval_today,
        "max_rounds": effective_max_rounds,
    }


def load_and_verify_checkpoint(
    checkpoint_file: Path,
    current_fingerprint: dict[str, Any],
) -> dict[str, Any]:
    """Load a checkpoint and refuse to resume if the configuration fingerprint differs."""
    if not checkpoint_file.exists():
        raise FileNotFoundError(f"Checkpoint file not found: {checkpoint_file}")

    data = json.loads(checkpoint_file.read_text())
    stored_fp = data.get("fingerprint", {})
    mismatches = []
    for key in [
        "policy_sha256",
        "seed_dump_sha256",
        "scenarios_sha256",
        "git_commit",
        "model",
        "temperature",
        "EVAL_TODAY",
        "max_rounds",
    ]:
        if stored_fp.get(key) != current_fingerprint.get(key):
            mismatches.append(
                f"{key} (checkpoint had '{stored_fp.get(key)}', current is '{current_fingerprint.get(key)}')"
            )

    if mismatches:
        raise RuntimeError(
            "Checkpoint fingerprint mismatch: refusing to resume run with different configuration:\n"
            + "\n".join(f"  - {m}" for m in mismatches)
        )

    return data


def save_checkpoint(
    checkpoint_file: Path,
    fingerprint: dict[str, Any],
    completed_runs: list[dict[str, Any]],
) -> None:
    """Save evaluation checkpoint and progress atomically."""
    data = {
        "fingerprint": fingerprint,
        "updated_at": datetime.now().isoformat(),
        "completed_runs": completed_runs,
    }
    checkpoint_file.write_text(json.dumps(data, indent=2))


def derive_policy_version() -> str:
    """Derive policy version dynamically from the policy file header."""
    policy_path = ROOT_DIR / "policy" / "policy.md"
    if not policy_path.exists():
        return "unknown"
    content = policy_path.read_text(encoding="utf-8")
    m = re.search(r"\(v([0-9]+(?:\.[0-9]+)?)\)", content)
    if m:
        return f"v{m.group(1)}"
    m = re.search(r"\bv([0-9]+(?:\.[0-9]+)?)\b", content, re.IGNORECASE)
    if m:
        return f"v{m.group(1)}"
    return "v2"


def get_harness_hashes() -> dict[str, str]:
    """Compute current SHA-256 hashes for policy, seed, scenarios, and heldout files."""
    policy_path = ROOT_DIR / "policy" / "policy.md"
    policy_sha256 = (
        hashlib.sha256(policy_path.read_bytes()).hexdigest()
        if policy_path.exists()
        else "none"
    )

    seed_sha256 = compute_seed_dump_sha256()

    scenarios_sha256 = compute_dir_sha256(SCENARIOS_DIR, "*.yaml")

    heldout_dir = ROOT_DIR / "scenarios_heldout"
    if not heldout_dir.exists():
        heldout_dir = ROOT_DIR / "heldout"
    heldout_sha256 = compute_dir_sha256(heldout_dir, "*.yaml")

    return {
        "policy_sha256": policy_sha256,
        "seed_sha256": seed_sha256,
        "scenarios_sha256": scenarios_sha256,
        "heldout_sha256": heldout_sha256,
    }


def get_pip_freeze_sha256() -> str:
    """Return SHA-256 hex digest of sorted pip freeze output."""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "freeze"],
            capture_output=True,
            text=True,
            check=True,
        )
        sorted_lines = sorted(proc.stdout.strip().splitlines())
        content = "\n".join(sorted_lines).encode("utf-8")
        return hashlib.sha256(content).hexdigest()
    except Exception:
        return ""


def get_openai_version() -> str:
    """Return the installed version of the openai package."""
    try:
        import openai
        return getattr(openai, "__version__", "unknown")
    except Exception:
        return "unknown"


def verify_requirements_lock() -> None:
    """Refuse to run unless installed package versions match requirements.lock exactly."""
    lock_file = ROOT_DIR / "requirements.lock"
    if not lock_file.exists():
        raise RuntimeError(f"requirements.lock not found at {lock_file}")

    expected: dict[str, str] = {}
    for line in lock_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "==" in line:
            pkg, ver = line.split("==", 1)
            expected[pkg.strip().lower().replace("_", "-")] = ver.strip()

    try:
        from importlib.metadata import distributions
        installed = {
            dist.metadata["Name"].lower().replace("_", "-"): dist.version
            for dist in distributions()
            if dist.metadata and dist.metadata.get("Name")
        }
    except Exception:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "freeze"],
            capture_output=True,
            text=True,
            check=True,
        )
        installed = {}
        for line in proc.stdout.splitlines():
            if "==" in line:
                pkg, ver = line.strip().split("==", 1)
                installed[pkg.lower().replace("_", "-")] = ver.strip()

    mismatches = []
    for pkg, exp_ver in sorted(expected.items()):
        inst_ver = installed.get(pkg)
        if inst_ver is None:
            mismatches.append(f"Missing package '{pkg}' (expected =={exp_ver})")
        elif inst_ver != exp_ver:
            mismatches.append(f"Package '{pkg}': expected =={exp_ver}, installed {inst_ver}")

    if mismatches:
        raise RuntimeError(
            "Installed packages do not match requirements.lock:\n  "
            + "\n  ".join(mismatches)
            + "\nRefusing to run. Please install from requirements.lock:\n  pip install -r requirements.lock"
        )


def get_git_info() -> tuple[str, bool]:
    """Retrieve current commit hash and whether the working tree is dirty."""
    try:
        commit_res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            cwd=str(ROOT_DIR),
        )
        commit_hash = commit_res.stdout.strip()
    except Exception:
        commit_hash = "unknown"

    try:
        status_res = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
            cwd=str(ROOT_DIR),
        )
        dirty = bool(status_res.stdout.strip())
    except Exception:
        dirty = False

    return commit_hash, dirty


def _check_git_tag(tag: str) -> None:
    """Verify that a git tag exists and is an ancestor of HEAD."""
    result = subprocess.run(
        ["git", "tag", "--list", tag],
        capture_output=True,
        text=True,
        cwd=str(ROOT_DIR),
    )
    tags = [t.strip() for t in result.stdout.strip().splitlines() if t.strip()]
    if tag not in tags:
        raise RuntimeError(f"Required git tag '{tag}' not found. Run: git tag {tag}")

    anc_result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", tag, "HEAD"],
        capture_output=True,
        cwd=str(ROOT_DIR),
    )
    if anc_result.returncode != 0:
        raise RuntimeError(f"Git tag '{tag}' is not an ancestor of HEAD.")


def _get_tag_blob(tag: str, relative_path: str) -> bytes:
    """Retrieve the content of a file at a specific git tag."""
    res = subprocess.run(
        ["git", "show", f"{tag}:{relative_path}"],
        capture_output=True,
        check=True,
        cwd=str(ROOT_DIR),
    )
    return res.stdout


def _get_tag_dir_sha256(tag: str, directory_rel: str) -> str:
    """Compute combined SHA-256 for all yaml files in a directory at a git tag."""
    res = subprocess.run(
        ["git", "ls-tree", "-r", tag, "--name-only", directory_rel],
        capture_output=True,
        text=True,
        cwd=str(ROOT_DIR),
    )
    if res.returncode != 0:
        return "none"
    files = sorted([
        line.strip()
        for line in res.stdout.strip().splitlines()
        if line.strip() and line.strip().endswith(".yaml")
    ])
    if not files:
        return "none"
    hasher = hashlib.sha256()
    for rel_path in files:
        fname = Path(rel_path).name
        blob = _get_tag_blob(tag, rel_path)
        hasher.update(fname.encode("utf-8"))
        hasher.update(blob)
    return hasher.hexdigest()


def verify_tag_gating(is_heldout: bool) -> None:
    """Enforce tag existence, ancestry, and content integrity before running evaluations."""
    if is_heldout:
        tag = "heldout-frozen"
        _check_git_tag(tag)
        heldout_dir = ROOT_DIR / "scenarios_heldout"
        curr_hash = compute_dir_sha256(heldout_dir, "*.yaml")
        tag_hash = _get_tag_dir_sha256(tag, "scenarios_heldout")
        if curr_hash != tag_hash:
            raise RuntimeError(
                f"Held-out scenarios have changed since tag '{tag}' (tag: {tag_hash}, current: {curr_hash})."
            )
    else:
        tag = "policy-v2-frozen"
        _check_git_tag(tag)
        policy_path = ROOT_DIR / "policy" / "policy.md"
        if not policy_path.exists():
            raise RuntimeError(f"Policy file not found at {policy_path}")
        curr_hash = hashlib.sha256(policy_path.read_bytes()).hexdigest()
        tag_blob = _get_tag_blob(tag, "policy/policy.md")
        tag_hash = hashlib.sha256(tag_blob).hexdigest()
        if curr_hash != tag_hash:
            raise RuntimeError(
                f"Policy file has changed since tag '{tag}' (tag: {tag_hash}, current: {curr_hash})."
            )
        diff_res = subprocess.run(
            [
                "git",
                "diff",
                "--exit-code",
                tag,
                "HEAD",
                "--",
                "app",
                "policy",
                "scenarios",
                "eval/run_eval.py",
                "requirements.txt",
                "requirements.lock",
                "eval/validate_scenarios.py",
                "Makefile",
            ],
            capture_output=True,
            text=True,
            cwd=str(ROOT_DIR),
        )
        if diff_res.returncode != 0:
            raise RuntimeError(
                f"Frozen files have changed since tag '{tag}'. "
                f"git diff --exit-code {tag} HEAD -- app policy scenarios eval/run_eval.py requirements.txt requirements.lock eval/validate_scenarios.py Makefile failed."
            )


def check_smoke_scenario_ids(scenario_ids: list[str]) -> None:
    """Refuse any scenario ID that exists in scenarios_heldout/ without reading that directory."""
    heldout_dir = ROOT_DIR / "scenarios_heldout"
    for sc_id in scenario_ids:
        # Check specific path existence via stat without reading directory contents or file contents
        if (heldout_dir / f"{sc_id}.yaml").is_file() or (heldout_dir / sc_id).is_file():
            raise RuntimeError(
                f"Scenario '{sc_id}' exists in scenarios_heldout/; --smoke refuses held-out scenarios."
            )


# ── Main runner ───────────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Run evaluation scenarios")
    parser.add_argument(
        "--variant",
        choices=["A", "B", "C", "all"],
        default="all",
        help="Variant to run (default: all)",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=3,
        help="Runs per scenario (default: 3)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit number of scenarios to evaluate (default: all)",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Allow running evaluation on a dirty git working tree",
    )
    parser.add_argument(
        "--run-id",
        type=str,
        default=None,
        help="Run identifier / directory name in eval/runs/ (default: timestamp)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume an evaluation run from checkpoint.json in run directory",
    )
    parser.add_argument(
        "--heldout",
        action="store_true",
        help="Evaluate held-out scenarios (scenarios_heldout/) instead of dev scenarios",
    )
    parser.add_argument(
        "--smoke",
        nargs="*",
        default=None,
        metavar="SCENARIO_ID",
        help="Run smoke test for specified scenario IDs, bypassing freeze gate. Writes to eval/runs/smoke_<id>/ with smoke=true.",
    )
    cli_args = parser.parse_args()

    git_commit, is_dirty = get_git_info()
    if is_dirty and not cli_args.allow_dirty:
        print(
            "\n[ERROR] Git working tree has uncommitted changes.\n"
            "        Commit or stash all changes before running evaluation for reproducibility,\n"
            "        or pass --allow-dirty to override.\n"
        )
        sys.exit(1)

    try:
        verify_requirements_lock()
    except RuntimeError as err:
        print(f"\n[ERROR] Environment validation failure:\n{err}\n")
        sys.exit(1)

    if cli_args.smoke is None:
        try:
            verify_tag_gating(cli_args.heldout)
        except RuntimeError as err:
            print(f"\n[ERROR] Tag gating failure: {err}\n")
            sys.exit(1)
    else:
        if cli_args.heldout:
            print("\n[ERROR] --smoke cannot be combined with --heldout.\n")
            sys.exit(1)
        if len(cli_args.smoke) > 0:
            try:
                check_smoke_scenario_ids(cli_args.smoke)
            except RuntimeError as err:
                print(f"\n[ERROR] {err}\n")
                sys.exit(1)

    raw_run_id = cli_args.run_id or datetime.now().strftime("%Y%m%d_%H%M%S")
    run_id = f"smoke_{raw_run_id}" if cli_args.smoke is not None else raw_run_id
    runs_dir = EVAL_DIR / "runs"
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    if cli_args.smoke is not None:
        (run_dir / ".smoke").touch()
    run_results_jsonl = run_dir / ("results_heldout.jsonl" if cli_args.heldout else "results.jsonl")
    run_results_md = run_dir / ("RESULTS_HELDOUT.md" if cli_args.heldout else "RESULTS.md")

    api_key = os.environ.get("GROQ_API_KEY") or os.environ.get("LLM_API_KEY")
    target_dir = SCENARIOS_DIR if cli_args.smoke is not None else (ROOT_DIR / "scenarios_heldout" if cli_args.heldout else SCENARIOS_DIR)
    if cli_args.heldout and cli_args.smoke is None and not target_dir.exists():
        print(f"\n[ERROR] Held-out scenarios directory not found: {target_dir}\n")
        sys.exit(1)

    scenarios = load_all_scenarios(target_dir)
    if cli_args.smoke is not None and len(cli_args.smoke) > 0:
        smoke_ids = set(cli_args.smoke)
        scenarios = [s for s in scenarios if s.get("id") in smoke_ids]
        if not scenarios:
            print(f"\n[ERROR] No matching scenarios found for --smoke: {cli_args.smoke}\n")
            sys.exit(1)
    if cli_args.limit:
        scenarios = scenarios[:cli_args.limit]
    set_name = "held-out" if cli_args.heldout else ("smoke" if cli_args.smoke is not None else "dev")
    print(f"Loaded {len(scenarios)} {set_name} scenarios from {target_dir}.")

    checkpoint_file = run_dir / "checkpoint.json"
    current_fingerprint = get_run_fingerprint(git_commit)
    pip_freeze_hash = get_pip_freeze_sha256()
    openai_version = get_openai_version()

    if not api_key:
        print(
            "\n[!] Notice: LLM_API_KEY / GROQ_API_KEY is not set.\n"
            "    To run live evaluation against Groq:\n"
            "        export GROQ_API_KEY='gsk_...'\n"
            "        export LLM_MODEL='llama-3.3-70b-versatile'   # default\n"
            "        make eval\n"
        )
        save_checkpoint(checkpoint_file, current_fingerprint, [])
        # Write placeholder RESULTS.md
        placeholder_md = (
            "# Evaluation Results\n\n"
            "> [!NOTE]\n"
            "> Real evaluation numbers require an active `LLM_API_KEY`. Set `LLM_API_KEY` and run `make eval`.\n\n"
            "## Variants Comparison (Placeholder)\n\n"
            "| Metric | (A) No Policy, Guard ON | (B) Policy in Prompt, Guard ON | (C) Policy in Prompt, Guard OFF |\n"
            "|---|---|---|---|\n"
            "| **Final-state success rate** | _pending eval_ | _pending eval_ | _pending eval_ |\n"
            "| **Consistency (all 3 runs passed)** | _pending eval_ | _pending eval_ | _pending eval_ |\n"
            "| **Policy-violation rate** | _pending eval_ | _pending eval_ | _pending eval_ |\n"
            "| **Escalation Precision** | _pending eval_ | _pending eval_ | _pending eval_ |\n"
            "| **Escalation Recall** | _pending eval_ | _pending eval_ | _pending eval_ |\n"
            "| **Avg turns per resolved scenario** | _pending eval_ | _pending eval_ | _pending eval_ |\n"
            "| **Avg tool calls per resolved scenario** | _pending eval_ | _pending eval_ | _pending eval_ |\n"
            "| **Avg latency per scenario** | _pending eval_ | _pending eval_ | _pending eval_ |\n"
            "| **Avg tokens per scenario** | _pending eval_ | _pending eval_ | _pending eval_ |\n\n"
            "## Most Frequent Scenario Failures\n\n"
            "_Pending evaluation run._\n"
        )
        run_results_md.write_text(placeholder_md)
        print(f"Wrote placeholder report to {run_results_md}.")
        if cli_args.smoke is None:
            latest_path = runs_dir / "latest"
            try:
                if latest_path.is_symlink() or latest_path.exists():
                    if latest_path.is_dir() and not latest_path.is_symlink():
                        import shutil
                        shutil.rmtree(latest_path)
                    else:
                        latest_path.unlink()
                latest_path.symlink_to(run_dir.resolve(), target_is_directory=True)
            except Exception:
                import shutil
                shutil.copytree(run_dir, latest_path, dirs_exist_ok=True)
        return

    # Variants definition
    all_variants = [
        {
            "id": "A",
            "name": "No policy, Guard ON",
            "include_policy": False,
            "guard": True,
        },
        {
            "id": "B",
            "name": "Policy in prompt, Guard ON",
            "include_policy": True,
            "guard": True,
        },
        {
            "id": "C",
            "name": "Policy in prompt, Guard OFF",
            "include_policy": True,
            "guard": False,
        },
    ]

    if cli_args.variant != "all":
        variants = [v for v in all_variants if v["id"] == cli_args.variant]
    else:
        variants = all_variants

    all_results: list[RunResult] = []
    variant_metrics: dict[str, dict[str, Any]] = {}
    jsonl_entries = []

    completed_runs_set: set[tuple[str, str, int]] = set()
    checkpoint_data: dict[str, Any]

    if cli_args.resume:
        try:
            checkpoint_data = load_and_verify_checkpoint(checkpoint_file, current_fingerprint)
        except Exception as e:
            print(f"\n[ERROR] {e}\n")
            sys.exit(1)
        for c in checkpoint_data.get("completed_runs", []):
            completed_runs_set.add((c["variant"], c["scenario_id"], c["run_num"]))
        print(f"Resuming run '{run_id}': skipping {len(completed_runs_set)} completed runs.")
    else:
        checkpoint_data = {
            "fingerprint": current_fingerprint,
            "created_at": datetime.now().isoformat(),
            "completed_runs": [],
        }
        save_checkpoint(checkpoint_file, current_fingerprint, [])
        run_results_jsonl.write_text("")

    try:
        for v in variants:
            print(f"\n========================================================")
            print(f"Evaluating Variant {v['id']}: {v['name']}")
            print(f"========================================================", flush=True)
            v_results: list[RunResult] = []
            for idx, sc in enumerate(scenarios, 1):
                sc_id = sc.get("id", f"scenario_{idx:02d}")
                for run_i in range(1, cli_args.runs + 1):
                    if (v["id"], sc_id, run_i) in completed_runs_set:
                        print(
                            f"  [{v['id']}] [{idx:02d}/{len(scenarios)}] {sc_id} (run {run_i}/{cli_args.runs})... SKIPPED (checkpoint)",
                            flush=True,
                        )
                        continue

                    print(
                        f"  [{v['id']}] [{idx:02d}/{len(scenarios)}] {sc_id} (run {run_i}/{cli_args.runs})... ",
                        end="",
                        flush=True,
                    )
                    res, trace = run_single_scenario(
                        scenario=sc,
                        variant_name=v["id"],
                        include_policy=v["include_policy"],
                        guard_enabled=v["guard"],
                        run_num=run_i,
                        smoke=(cli_args.smoke is not None),
                    )
                    status_str = "PASS" if res.success else "FAIL"
                    if res.policy_violation:
                        status_str += " (policy-violation)"
                    print(f"{status_str} [{res.latency_sec:.1f}s]", flush=True)

                    v_results.append(res)
                    all_results.append(res)
                    harness_hashes = get_harness_hashes()

                    entry = {
                        "scenario_id": res.scenario_id,
                        "category": res.category,
                        "group": res.group,
                        "variant": res.variant,
                        "run_num": res.run_num,
                        "success": res.success,
                        "db_matches": res.db_matches,
                        "forbidden_triggered": res.forbidden_triggered,
                        "blocked_violation_count": res.blocked_violation_count,
                        "policy_violation": res.policy_violation,
                        "should_escalate": res.should_escalate,
                        "did_escalate": res.did_escalate,
                        "failure_reasons": res.failure_reasons,
                        "latency_sec": res.latency_sec,
                        "tokens": res.tokens,
                        "tool_errors": res.tool_errors,
                        "smoke": res.smoke,
                        "eval_today": res.eval_today,
                        "policy_version": derive_policy_version(),
                        "policy_sha256": harness_hashes["policy_sha256"],
                        "seed_sha256": harness_hashes["seed_sha256"],
                        "scenarios_sha256": harness_hashes["scenarios_sha256"],
                        "heldout_sha256": harness_hashes["heldout_sha256"],
                        "pip_freeze_sha256": pip_freeze_hash,
                        "openai_version": openai_version,
                        "openai_client_version": openai_version,
                        "sys_executable": sys.executable,
                        "python_version": platform.python_version(),
                        "model": current_fingerprint["model"],
                        "temperature": current_fingerprint["temperature"],
                        "max_rounds": current_fingerprint["max_rounds"],
                        "git_commit": git_commit,
                        "dirty": is_dirty,
                    }
                    jsonl_entries.append(entry)
                    with open(run_results_jsonl, "a") as fp:
                        fp.write(json.dumps(entry) + "\n")

                    checkpoint_data.setdefault("completed_runs", []).append(
                        {
                            "variant": res.variant,
                            "scenario_id": res.scenario_id,
                            "run_num": res.run_num,
                            "success": res.success,
                        }
                    )
                    save_checkpoint(
                        checkpoint_file,
                        current_fingerprint,
                        checkpoint_data["completed_runs"],
                    )

                    # Brief 0.5s pause to avoid slamming Groq token-per-minute rate limit
                    time.sleep(0.5)

            main_results = [r for r in v_results if r.group != "ambiguous"]
            metrics = compute_metrics(main_results)
            ambiguous_results = [r for r in v_results if r.group == "ambiguous"]
            if ambiguous_results:
                metrics["ambiguous_metrics"] = compute_metrics(ambiguous_results)
            variant_metrics[v["id"]] = metrics
            print(
                f"\n>>> Variant {v['id']} Finished: Success {metrics['success_rate']:.1f}% | "
                f"Consistency {metrics['consistency_rate']:.1f}% | "
                f"Violations {metrics['violation_rate']:.1f}%",
                flush=True,
            )
    except Exception as e:
        if "RateLimitError" in type(e).__name__ or "rate limit" in str(e).lower():
            print(f"\n[!] API Rate limit reached: {e}")
            print(
                "\nExplanation: Experimental models like 'gpt-6-luna' often have strict daily limits (e.g. 50 RPD)."
            )
            print(
                "A full evaluation of 30 scenarios x 3 runs x 3 variants requires ~270-500 requests."
            )
            print("To resolve:")
            print("1. Switch to a model with standard rate limits:")
            print("   export LLM_MODEL='gpt-4o-mini'")
            print("   make eval")
            print("\n2. Or test a smaller subset on your current quota:")
            print("   python3 eval/run_eval.py --variant B --runs 1 --limit 5")
            return
        raise

    # Determine most frequent failures across runs
    failure_counts: dict[str, list[str]] = {}
    for r in all_results:
        if not r.success:
            for reason in r.failure_reasons:
                failure_counts.setdefault(r.scenario_id, []).append(reason)

    sc_lookup = {s["id"]: s.get("description", "") for s in scenarios}
    frequent_failures = []
    for sc_id, reasons in sorted(
        failure_counts.items(), key=lambda item: len(item[1]), reverse=True
    )[:5]:
        most_common_reason = max(set(reasons), key=reasons.count)
        frequent_failures.append({
            "scenario_id": sc_id,
            "description": sc_lookup.get(sc_id, ""),
            "reason": most_common_reason,
        })

    # Write RESULTS.md into run directory
    report_md = format_results_markdown(variant_metrics, frequent_failures)
    run_results_md.write_text(report_md)
    print(f"\nWrote full evaluation report to {run_results_md}")

    # Write complete run records
    with open(run_results_jsonl, "w") as fp:
        for entry in jsonl_entries:
            fp.write(json.dumps(entry) + "\n")
    print(f"Wrote {len(jsonl_entries)} raw run records to {run_results_jsonl}")

    # Update latest symlink / directory in eval/runs/ (never for smoke runs)
    if cli_args.smoke is None:
        latest_path = runs_dir / "latest"
        try:
            if latest_path.is_symlink() or latest_path.exists():
                if latest_path.is_dir() and not latest_path.is_symlink():
                    import shutil
                    shutil.rmtree(latest_path)
                else:
                    latest_path.unlink()
            latest_path.symlink_to(run_dir.resolve(), target_is_directory=True)
        except Exception:
            import shutil
            shutil.copytree(run_dir, latest_path, dirs_exist_ok=True)



if __name__ == "__main__":
    main()
