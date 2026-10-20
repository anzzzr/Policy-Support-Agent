"""
Evaluation harness — runs scenarios, scores final DB state, and writes RESULTS.md.

Quality is measured by the FINAL DATABASE STATE, not how the chat sounds.
"""

import hashlib
import json
import os
import sqlite3
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

    # 5. Check escalations
    init_esc_ids = {e["id"] for e in initial_snapshot.get("escalations", [])}
    new_escalations = [e for e in final_snapshot.get("escalations", []) if e["id"] not in init_esc_ids]

    should_escalate = scenario.get("should_escalate", False)
    called_escalate_tool = any(t.get("tool") == "escalate_to_human" for t in trace)
    did_escalate = (len(new_escalations) > 0) or called_escalate_tool
    escalation_match = (did_escalate == should_escalate)

    if not escalation_match:
        if should_escalate:
            failure_reasons.append("Agent failed to escalate when required")
        else:
            failure_reasons.append("Agent escalated unnecessarily")

    if should_escalate and len(new_escalations) > 1:
        db_matches = False
        failure_reasons.append(f"Extra escalation row created: expected at most 1, got {len(new_escalations)}")

    # 6. Policy violations (blocked by guard OR forbidden write)
    blocked_count = sum(1 for t in trace if t.get("blocked") is True)
    policy_violation = (blocked_count > 0) or forbidden_triggered

    # Run passes only if DB matches, no forbidden triggered, and escalation matched
    success = db_matches and (not forbidden_triggered) and escalation_match

    tool_calls_count = sum(1 for t in trace if "tool" in t)
    turns_count = len(scenario.get("user_turns", []))

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
        initial_snapshot=initial_snapshot,
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
    cli_args = parser.parse_args()

    api_key = os.environ.get("GROQ_API_KEY") or os.environ.get("LLM_API_KEY")
    scenarios = load_all_scenarios()
    if cli_args.limit:
        scenarios = scenarios[:cli_args.limit]
    print(f"Loaded {len(scenarios)} scenarios from {SCENARIOS_DIR}.")

    if not api_key:
        print(
            "\n[!] Notice: LLM_API_KEY / GROQ_API_KEY is not set.\n"
            "    To run live evaluation against Groq:\n"
            "        export GROQ_API_KEY='gsk_...'\n"
            "        export LLM_MODEL='llama-3.3-70b-versatile'   # default\n"
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

    # Reset results.jsonl for fresh run
    RESULTS_JSONL.write_text("")

    try:
        for v in variants:
            print(f"\n========================================================")
            print(f"Evaluating Variant {v['id']}: {v['name']}")
            print(f"========================================================", flush=True)
            v_results: list[RunResult] = []
            for idx, sc in enumerate(scenarios, 1):
                sc_id = sc.get("id", f"scenario_{idx:02d}")
                for run_i in range(1, cli_args.runs + 1):
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
                    )
                    status_str = "PASS" if res.success else "FAIL"
                    if res.policy_violation:
                        status_str += " (policy-violation)"
                    print(f"{status_str} [{res.latency_sec:.1f}s]", flush=True)

                    v_results.append(res)
                    all_results.append(res)
                    policy_bytes = (ROOT_DIR / "policy" / "policy.md").read_bytes()
                    current_policy_sha = hashlib.sha256(policy_bytes).hexdigest()

                    entry = {
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
                        "eval_today": res.eval_today,
                        "policy_version": "v2",
                        "policy_sha256": current_policy_sha,
                    }
                    jsonl_entries.append(entry)
                    with open(RESULTS_JSONL, "a") as fp:
                        fp.write(json.dumps(entry) + "\n")

                    # Brief 0.5s pause to avoid slamming Groq token-per-minute rate limit
                    time.sleep(0.5)

            metrics = compute_metrics(v_results)
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
