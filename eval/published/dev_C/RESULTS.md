# Evaluation Results

- **Distinct Scenarios:** 45
- **Runs per Scenario:** 3
- **Total Runs:** 135
- **Variants Present:** C
- **Model:** nemotron-3-nano-30b
- **Date:** 2026-10-22 06:07:21
- **Git Commit:** 20f389720b43a380642eb04907b0f56b058893b5
- **Dirty:** False
- **Policy Hash:** 32f15e54289083fae9830c97f90682370036d4152573141210a4cd3570a8b771

Evaluation results across 45 scenarios (135 runs total).
Quality is measured by the **final database state**, not conversation style.

## Variants Comparison

| Metric | (A) No Policy in Prompt, Guard ON | (B) Policy in Prompt, Guard ON | (C) Policy in Prompt, Guard OFF |
|---|---|---|---|
| **Final-state success rate** | N/A | N/A | 90.4% (122/135) |
| **Consistency** | N/A | N/A | 82.2% (37/45) |
| **Policy-violation rate** | N/A | N/A | 0.7% (1/135) |
| **Escalation Precision** | N/A | N/A | 83.3% (30/36) |
| **Escalation Recall** | N/A | N/A | 90.9% (30/33) |
| **Avg turns per resolved scenario** | N/A | N/A | 1.4 |
| **Avg tool calls per resolved scenario** | N/A | N/A | 1.8 |
| **Avg latency per scenario (total)** | N/A | N/A | 12.72s |
| **Avg latency per scenario (excl. rate-limit waits)** | N/A | N/A | 12.72s |
| **Total tool errors** | N/A | N/A | 2 |
| **Total retries** | N/A | N/A | 0 |
| **Avg prompt tokens per scenario** | N/A | N/A | 8893 |
| **Avg completion tokens per scenario** | N/A | N/A | 2678 |
| **Avg tokens per scenario** | N/A | N/A | 11571 |

## Most Frequent Scenario Failures

- **scenario_30**: Expected escalation on turn 1, but no successful escalate_to_human call occurred
- **scenario_05**: Agent escalated unnecessarily
- **scenario_02**: Expected refund for order 1010 not found
- **scenario_03**: Order 1012 field 'status' expected 'refunded', got 'delivered'
- **scenario_22**: Agent escalated unnecessarily
