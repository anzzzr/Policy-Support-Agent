#!/usr/bin/env python3
"""
Summarize published evaluation runs across variants and scenario groups.

Reads all eval/published/*/*.jsonl files, groups rows by (variant, group),
and generates eval/published/SUMMARY.md with statistical rigor (Wilson score interval).
"""

from __future__ import annotations

import argparse
import glob
import json
import math
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
PUBLISHED_DIR = ROOT_DIR / "eval" / "published"
OUTPUT_FILE = PUBLISHED_DIR / "SUMMARY.md"


def wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Calculate 95% Wilson score confidence interval for a binomial proportion.

    Formula:
      center = (p + z^2 / (2N)) / (1 + z^2 / N)
      margin = (z / (1 + z^2 / N)) * sqrt(p(1-p)/N + z^2 / (4N^2))
    """
    if total <= 0:
        return 0.0, 0.0
    p = successes / total
    z2 = z * z
    denom = 1.0 + z2 / total
    center = (p + z2 / (2.0 * total)) / denom
    margin = (z / denom) * math.sqrt((p * (1.0 - p) / total) + (z2 / (4.0 * total * total)))
    lower = max(0.0, center - margin)
    upper = min(1.0, center + margin)
    return lower, upper


def load_published_rows(directory: Path | str = PUBLISHED_DIR) -> list[dict[str, Any]]:
    """Load and filter result records from published jsonl files.

    Skips rows with smoke=true and rows outside recognized groups (dev, ambiguous, heldout).
    """
    directory = Path(directory)
    pattern = str(directory / "*" / "*.jsonl")
    matched_files = sorted(glob.glob(pattern))

    rows: list[dict[str, Any]] = []
    for filepath in matched_files:
        p = Path(filepath)
        # Skip legacy directory to prevent mixing historical v1 runs
        if p.parent.name == "legacy":
            continue
        try:
            with open(p, "r", encoding="utf-8") as fp:
                for line in fp:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    # Filter out smoke runs
                    if record.get("smoke") is True:
                        continue

                    # Require valid group
                    group = record.get("group")
                    if group not in ("dev", "ambiguous", "heldout"):
                        continue

                    rows.append(record)
        except OSError:
            continue

    return rows


def summarize_group(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute aggregate statistics for a specific (variant, group) cohort."""
    total_runs = len(rows)
    if total_runs == 0:
        return {}

    # Success rate & Wilson interval
    successes = sum(1 for r in rows if r.get("success") is True)
    success_rate = (successes / total_runs) * 100.0 if total_runs else 0.0
    ci_lower, ci_upper = wilson_interval(successes, total_runs)

    # Consistency across scenarios
    scenario_runs: dict[str, list[bool]] = defaultdict(list)
    for r in rows:
        sc_id = r.get("scenario_id", "unknown")
        scenario_runs[sc_id].append(bool(r.get("success")))

    total_scenarios = len(scenario_runs)
    consistent_scenarios = sum(1 for runs in scenario_runs.values() if all(runs))
    min_runs_per_sc = min(len(runs) for runs in scenario_runs.values()) if scenario_runs else 0
    consistency_rate = (consistent_scenarios / total_scenarios * 100.0) if total_scenarios else 0.0

    # Policy violations
    violations = sum(1 for r in rows if r.get("policy_violation") is True)
    violation_rate = (violations / total_runs * 100.0) if total_runs else 0.0

    # Escalation Precision & Recall
    escalated = [r for r in rows if r.get("did_escalate") is True]
    tp = sum(1 for r in escalated if r.get("should_escalate") is True)
    total_escalated = len(escalated)
    precision_pct = (tp / total_escalated * 100.0) if total_escalated > 0 else None

    expected_escalations = [r for r in rows if r.get("should_escalate") is True]
    caught = sum(1 for r in expected_escalations if r.get("did_escalate") is True)
    total_expected = len(expected_escalations)
    recall_pct = (caught / total_expected * 100.0) if total_expected > 0 else None

    # Latency & tokens
    avg_latency = sum(r.get("latency_sec", 0.0) for r in rows) / total_runs
    avg_tokens = sum(r.get("tokens", 0) for r in rows) / total_runs

    # Tool errors
    total_tool_errors = sum(r.get("tool_errors", {}).get("total", 0) for r in rows)

    # Failed scenarios and their failure reasons
    failed_scenarios: dict[str, list[str]] = defaultdict(list)
    scenario_fail_counts: Counter[str] = Counter()
    for r in rows:
        sc_id = r.get("scenario_id", "unknown")
        if not r.get("success"):
            scenario_fail_counts[sc_id] += 1
            reasons = r.get("failure_reasons", [])
            if not reasons:
                reasons = ["Scenario did not meet expected final state"]
            for reason in reasons:
                if reason not in failed_scenarios[sc_id]:
                    failed_scenarios[sc_id].append(reason)

    return {
        "total_runs": total_runs,
        "successes": successes,
        "success_rate": success_rate,
        "ci_lower": ci_lower * 100.0,
        "ci_upper": ci_upper * 100.0,
        "total_scenarios": total_scenarios,
        "consistent_scenarios": consistent_scenarios,
        "min_runs_per_sc": min_runs_per_sc,
        "consistency_rate": consistency_rate,
        "violations": violations,
        "violation_rate": violation_rate,
        "tp": tp,
        "total_escalated": total_escalated,
        "precision_pct": precision_pct,
        "caught": caught,
        "total_expected": total_expected,
        "recall_pct": recall_pct,
        "avg_latency": avg_latency,
        "avg_tokens": avg_tokens,
        "total_tool_errors": total_tool_errors,
        "failed_scenarios": failed_scenarios,
        "scenario_fail_counts": scenario_fail_counts,
        "scenario_runs": scenario_runs,
    }


def generate_summary_markdown(
    rows: list[dict[str, Any]],
    metadata: dict[str, str] | None = None,
) -> str:
    """Format evaluation statistics into a clean, comprehensive SUMMARY.md report."""
    if not rows:
        return "# Evaluation Summary\n\n_No published evaluation runs found._\n"

    # Extract metadata from rows if not explicitly provided
    first_row = rows[0]
    model = (metadata or {}).get("model") or first_row.get("model", "unknown")
    policy_hash = (metadata or {}).get("policy_hash") or first_row.get("policy_sha256", "none")
    git_commit = (metadata or {}).get("git_commit") or first_row.get("git_commit", "unknown")
    date_str = (metadata or {}).get("date") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines: list[str] = [
        "# Evaluation Summary",
        "",
        f"- **Model:** `{model}`",
        f"- **Policy Hash:** `{policy_hash}`",
        f"- **Git Commit:** `{git_commit}`",
        f"- **Date:** {date_str}",
        "",
        "## Overview Table",
        "",
        "| Variant | Group | Success (95% Wilson CI) | Consistency | Violations | Esc. Precision | Esc. Recall | Avg Tokens | Avg Latency | Tool Errors |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]

    # Group rows by (variant, group)
    grouped_rows: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        v = r.get("variant", "unknown")
        g = r.get("group", "unknown")
        grouped_rows[(v, g)].append(r)

    # Sort cohorts: primary variants (B, C, A), then groups (dev, ambiguous, heldout)
    variant_order = {"B": 0, "C": 1, "A": 2}
    group_order = {"dev": 0, "ambiguous": 1, "heldout": 2}

    sorted_cohorts = sorted(
        grouped_rows.keys(),
        key=lambda vg: (variant_order.get(vg[0], 99), vg[0], group_order.get(vg[1], 99), vg[1]),
    )

    summaries: dict[tuple[str, str], dict[str, Any]] = {}
    for vg in sorted_cohorts:
        cohort_rows = grouped_rows[vg]
        summary = summarize_group(cohort_rows)
        summaries[vg] = summary
        v, g = vg

        # Format Wilson CI
        succ_str = f"{summary['success_rate']:.1f}% ({summary['successes']}/{summary['total_runs']}, [{summary['ci_lower']:.1f}%, {summary['ci_upper']:.1f}%])"

        # Consistency
        if summary["min_runs_per_sc"] >= 2:
            cons_str = f"{summary['consistency_rate']:.1f}% ({summary['consistent_scenarios']}/{summary['total_scenarios']})"
        else:
            cons_str = f"{summary['consistency_rate']:.1f}% ({summary['consistent_scenarios']}/{summary['total_scenarios']})*"

        # Violations
        viol_str = f"{summary['violation_rate']:.1f}% ({summary['violations']}/{summary['total_runs']})"

        # Precision & Recall
        prec_str = (
            f"{summary['precision_pct']:.1f}% ({summary['tp']}/{summary['total_escalated']})"
            if summary["precision_pct"] is not None
            else "N/A"
        )
        rec_str = (
            f"{summary['recall_pct']:.1f}% ({summary['caught']}/{summary['total_expected']})"
            if summary["recall_pct"] is not None
            else "N/A"
        )

        tok_str = f"{summary['avg_tokens']:,.0f}"
        lat_str = f"{summary['avg_latency']:.2f}s"
        err_str = str(summary["total_tool_errors"])

        lines.append(
            f"| **{v}** | `{g}` | {succ_str} | {cons_str} | {viol_str} | {prec_str} | {rec_str} | {tok_str} | {lat_str} | {err_str} |"
        )

    lines.append("")
    lines.append(r"\* *Note: Cohorts with 1 run per scenario report single-run pass consistency.*")
    lines.append("")
    lines.append("## Cohort Breakdown and Failure Logs")
    lines.append("")

    variant_descriptions = {
        "A": "Variant A (No Policy in Prompt, Guard ON)",
        "B": "Variant B (Policy in Prompt, Guard ON)",
        "C": "Variant C (Policy in Prompt, Guard OFF)",
    }

    for vg in sorted_cohorts:
        v, g = vg
        s = summaries[vg]
        v_title = variant_descriptions.get(v, f"Variant {v}")
        lines.append(f"### {v_title} — Group `{g}`")
        lines.append("")
        lines.append(f"- **Final-State Success:** {s['success_rate']:.1f}% ({s['successes']}/{s['total_runs']}) with 95% Wilson CI: [{s['ci_lower']:.1f}%, {s['ci_upper']:.1f}%]")
        if s["min_runs_per_sc"] >= 2:
            lines.append(f"- **Consistency:** {s['consistency_rate']:.1f}% ({s['consistent_scenarios']}/{s['total_scenarios']} scenarios passed all runs)")
        else:
            lines.append(f"- **Consistency:** {s['consistency_rate']:.1f}% ({s['consistent_scenarios']}/{s['total_scenarios']} scenarios passed single run)")
        lines.append(f"- **Policy-Violation Rate:** {s['violation_rate']:.1f}% ({s['violations']}/{s['total_runs']})")
        if s["precision_pct"] is not None:
            lines.append(f"- **Escalation Precision:** {s['precision_pct']:.1f}% ({s['tp']}/{s['total_escalated']})")
        else:
            lines.append("- **Escalation Precision:** N/A (0 escalations performed)")
        if s["recall_pct"] is not None:
            lines.append(f"- **Escalation Recall:** {s['recall_pct']:.1f}% ({s['caught']}/{s['total_expected']})")
        else:
            lines.append("- **Escalation Recall:** N/A (0 escalations expected)")
        lines.append(f"- **Average Latency:** {s['avg_latency']:.2f}s")
        lines.append(f"- **Average Tokens:** {s['avg_tokens']:,.0f}")
        lines.append(f"- **Total Tool Errors:** {s['total_tool_errors']}")
        lines.append("")

        failed = s["failed_scenarios"]
        fail_counts = s["scenario_fail_counts"]
        sc_runs = s["scenario_runs"]
        if failed:
            lines.append("**Failed Scenarios & Reasons:**")
            for sc_id in sorted(failed.keys()):
                fc = fail_counts[sc_id]
                tot = len(sc_runs[sc_id])
                reasons_text = "; ".join(failed[sc_id])
                lines.append(f"- **`{sc_id}`** ({fc}/{tot} runs failed): {reasons_text}")
        else:
            lines.append("_All scenarios in this cohort passed perfectly without failures._")
        lines.append("")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize published evaluation results")
    parser.add_argument(
        "--published-dir",
        type=Path,
        default=PUBLISHED_DIR,
        help=f"Directory containing published evaluation runs (default: {PUBLISHED_DIR})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=OUTPUT_FILE,
        help=f"Destination markdown summary file (default: {OUTPUT_FILE})",
    )
    args = parser.parse_args()

    rows = load_published_rows(args.published_dir)
    if not rows:
        print(f"No published evaluation records found in {args.published_dir}/*/*.jsonl")
        return

    report = generate_summary_markdown(rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    print(f"Wrote evaluation summary to {args.output}")


if __name__ == "__main__":
    main()
