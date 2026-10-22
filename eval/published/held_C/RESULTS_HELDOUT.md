# Evaluation Results

- **Distinct Scenarios:** 7
- **Runs per Scenario:** 3
- **Total Runs:** 21
- **Variants Present:** C
- **Model:** nemotron-3-nano-30b
- **Date:** 2026-10-22 06:28:16
- **Git Commit:** 20f389720b43a380642eb04907b0f56b058893b5
- **Dirty:** False
- **Policy Hash:** 32f15e54289083fae9830c97f90682370036d4152573141210a4cd3570a8b771

Evaluation results across 7 scenarios (21 runs total).
Quality is measured by the **final database state**, not conversation style.

## Variants Comparison

| Metric | (A) No Policy in Prompt, Guard ON | (B) Policy in Prompt, Guard ON | (C) Policy in Prompt, Guard OFF |
|---|---|---|---|
| **Final-state success rate** | N/A | N/A | 90.5% (19/21) |
| **Consistency** | N/A | N/A | 85.7% (6/7) |
| **Policy-violation rate** | N/A | N/A | 0.0% (0/21) |
| **Escalation Precision** | N/A | N/A | 100.0% (4/4) |
| **Escalation Recall** | N/A | N/A | 66.7% (4/6) |
| **Avg turns per resolved scenario** | N/A | N/A | 1.0 |
| **Avg tool calls per resolved scenario** | N/A | N/A | 2.0 |
| **Avg latency per scenario (total)** | N/A | N/A | 17.68s |
| **Avg latency per scenario (excl. rate-limit waits)** | N/A | N/A | 17.68s |
| **Total tool errors** | N/A | N/A | 0 |
| **Total retries** | N/A | N/A | 0 |
| **Avg prompt tokens per scenario** | N/A | N/A | 8228 |
| **Avg completion tokens per scenario** | N/A | N/A | 4421 |
| **Avg tokens per scenario** | N/A | N/A | 12649 |

## Most Frequent Scenario Failures

- **heldout_03**: Expected escalation on turn 1, but no successful escalate_to_human call occurred
