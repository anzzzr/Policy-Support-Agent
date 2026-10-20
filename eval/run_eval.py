"""
Evaluation harness — runs scenarios, scores final DB state, and writes RESULTS.md.

Quality is measured by the FINAL DATABASE STATE, not how the chat sounds.
"""

import json
import os
import sys
import time
from dataclasses import dataclass, field
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
    failure_reasons: list[str] = field(default_factory=list)


def evaluate_run(
    conn,
    scenario: dict,
    trace: list[dict],
    variant: str,
    run_num: int,
    latency_sec: float = 0.0,
    tokens: int = 0,
) -> RunResult:
    """Compare final database state against expected_db and forbidden rules."""
    failure_reasons = []
    expected_db = scenario.get("expected_db", {})
    forbidden = scenario.get("forbidden", {})
    should_escalate = scenario.get("should_escalate", False)

    # 1. Check expected order states
    db_matches = True
    for exp_order in expected_db.get("orders", []):
        order_id = exp_order["id"]
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if not row:
            db_matches = False
            failure_reasons.append(f"Order {order_id} missing in DB")
            continue
        order_dict = dict(row)
        for key, expected_val in exp_order.items():
            actual_val = order_dict.get(key)
            if actual_val != expected_val:
                db_matches = False
                failure_reasons.append(
                    f"Order {order_id} field '{key}' expected '{expected_val}', got '{actual_val}'"
                )

    # 2. Check expected refunds
    for exp_refund in expected_db.get("refunds", []):
        order_id = exp_refund["order_id"]
        rows = conn.execute(
            "SELECT * FROM refunds WHERE order_id = ?", (order_id,)
        ).fetchall()
        if not rows:
            db_matches = False
            failure_reasons.append(f"Expected refund for order {order_id} not found")
        elif "amount" in exp_refund:
            expected_amt = float(exp_refund["amount"])
            actual_amt = float(rows[-1]["amount"])
            if actual_amt != expected_amt:
                db_matches = False
                failure_reasons.append(
                    f"Refund amount for order {order_id} expected {expected_amt}, got {actual_amt}"
                )

    # 3. Check forbidden conditions
    forbidden_triggered = False

    # Forbidden refunds (orders that must not be refunded)
    for forbidden_order_id in forbidden.get("refunds", []):
        # Ignore initial seed refunds on 1008 and 1022
        rows = conn.execute(
            "SELECT * FROM refunds WHERE order_id = ? AND id > 2", (forbidden_order_id,)
        ).fetchall()
        if rows:
            forbidden_triggered = True
            failure_reasons.append(
                f"Forbidden refund was issued for order {forbidden_order_id}"
            )

    # Forbidden order states (e.g. order cancelled when it should not be)
    for forb_order in forbidden.get("orders", []):
        order_id = forb_order["id"]
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if row:
            order_dict = dict(row)
            for k, forb_v in forb_order.items():
                if k != "id" and order_dict.get(k) == forb_v:
                    forbidden_triggered = True
                    failure_reasons.append(
                        f"Forbidden state reached: order {order_id} has {k}='{forb_v}'"
                    )

    # 4. Check escalations
    new_escalations = conn.execute(
        "SELECT COUNT(*) FROM escalations"
    ).fetchone()[0]
    called_escalate_tool = any(t.get("tool") == "escalate_to_human" for t in trace)
    did_escalate = (new_escalations > 0) or called_escalate_tool
    escalation_match = (did_escalate == should_escalate)
    if not escalation_match:
        if should_escalate:
            failure_reasons.append("Agent failed to escalate when required")
        else:
            failure_reasons.append("Agent escalated unnecessarily")

    # 5. Policy violations (blocked by guard OR forbidden write)
    blocked_count = sum(1 for t in trace if t.get("blocked") is True)
    policy_violation = (blocked_count > 0) or forbidden_triggered

    # Run passes only if DB matches, no forbidden triggered, and escalation matched
    success = db_matches and (not forbidden_triggered) and escalation_match

    tool_calls_count = sum(1 for t in trace if "tool" in t)
    turns_count = len(scenario.get("user_turns", []))

    return RunResult(
        scenario_id=scenario.get("id", "unknown"),
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
) -> tuple[RunResult, list[dict]]:
    """Execute one run of a scenario against an isolated DB."""
    # Configure environment for this run
    os.environ["GUARD"] = "on" if guard_enabled else "off"
    os.environ["MOCK_DATE"] = "2026-10-21"

    conn = get_db(":memory:")
    seed(conn)

    system_prompt = build_system_prompt(include_policy=include_policy)
    messages = [{"role": "system", "content": system_prompt}]
    trace: list[dict] = []
    metrics: dict = {"tokens": 0}

    start_time = time.time()
    for user_turn in scenario.get("user_turns", []):
        messages.append({"role": "user", "content": user_turn})
        run_turn(conn, messages, trace, chat_fn=chat_fn, metrics=metrics)
    duration = time.time() - start_time

    result = evaluate_run(
        conn,
        scenario,
        trace,
        variant=variant_name,
        run_num=run_num,
        latency_sec=duration,
        tokens=metrics.get("tokens", 0),
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

    lines.append("")
    return "\n".join(lines)


# ── Main runner ───────────────────────────────────────────────────────

def main():
    api_key = os.environ.get("LLM_API_KEY")
    scenarios = load_all_scenarios()
    print(f"Loaded {len(scenarios)} scenarios from {SCENARIOS_DIR}.")

    if not api_key:
        print(
            "\n[!] Notice: LLM_API_KEY is not set.\n"
            "    To run live evaluation against an LLM provider:\n"
            "        export LLM_API_KEY='your-api-key'\n"
            "        export LLM_MODEL='gpt-4o-mini'   # or chosen model\n"
            "        make eval\n"
        )
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
        EVAL_DIR.mkdir(exist_ok=True)
        RESULTS_MD.write_text(placeholder_md)
        print(f"Wrote placeholder report to {RESULTS_MD}.")
        return

    # Variants definition
    variants = [
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

    all_results: list[RunResult] = []
    variant_metrics: dict[str, dict[str, Any]] = {}
    jsonl_entries = []

    for v in variants:
        print(f"\nEvaluating Variant {v['id']}: {v['name']}...")
        v_results: list[RunResult] = []
        for sc in scenarios:
            for run_i in range(1, 4):
                res, trace = run_single_scenario(
                    scenario=sc,
                    variant_name=v["id"],
                    include_policy=v["include_policy"],
                    guard_enabled=v["guard"],
                    run_num=run_i,
                )
                v_results.append(res)
                all_results.append(res)
                jsonl_entries.append({
                    "scenario_id": res.scenario_id,
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
                })
        metrics = compute_metrics(v_results)
        variant_metrics[v["id"]] = metrics
        print(
            f"  Variant {v['id']} Success Rate: {metrics['success_rate']:.1f}% | "
            f"Consistency: {metrics['consistency_rate']:.1f}% | "
            f"Violations: {metrics['violation_rate']:.1f}%"
        )

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

    # Write RESULTS.md
    report_md = format_results_markdown(variant_metrics, frequent_failures)
    EVAL_DIR.mkdir(exist_ok=True)
    RESULTS_MD.write_text(report_md)
    print(f"\nWrote full evaluation report to {RESULTS_MD}")

    # Write raw results.jsonl
    with open(RESULTS_JSONL, "w") as fp:
        for entry in jsonl_entries:
            fp.write(json.dumps(entry) + "\n")
    print(f"Wrote {len(jsonl_entries)} raw run records to {RESULTS_JSONL}")


if __name__ == "__main__":
    main()
