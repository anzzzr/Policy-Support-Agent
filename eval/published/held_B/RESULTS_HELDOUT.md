# Evaluation Results

- **Distinct Scenarios:** 7
- **Runs per Scenario:** 3
- **Total Runs:** 21
- **Variants Present:** B
- **Model:** nemotron-3-nano-30b
- **Date:** 2026-10-22 06:21:53
- **Git Commit:** 20f389720b43a380642eb04907b0f56b058893b5
- **Dirty:** False
- **Policy Hash:** 32f15e54289083fae9830c97f90682370036d4152573141210a4cd3570a8b771

Evaluation results across 7 scenarios (21 runs total).
Quality is measured by the **final database state**, not conversation style.

## Variants Comparison

| Metric | (A) No Policy in Prompt, Guard ON | (B) Policy in Prompt, Guard ON | (C) Policy in Prompt, Guard OFF |
|---|---|---|---|
| **Final-state success rate** | N/A | 90.5% (19/21) | N/A |
| **Consistency** | N/A | 71.4% (5/7) | N/A |
| **Policy-violation rate** | N/A | 0.0% (0/21) | N/A |
| **Escalation Precision** | N/A | 100.0% (5/5) | N/A |
| **Escalation Recall** | N/A | 83.3% (5/6) | N/A |
| **Avg turns per resolved scenario** | N/A | 1.0 | N/A |
| **Avg tool calls per resolved scenario** | N/A | 1.7 | N/A |
| **Avg latency per scenario (total)** | N/A | 16.92s | N/A |
| **Avg latency per scenario (excl. rate-limit waits)** | N/A | 16.92s | N/A |
| **Total tool errors** | N/A | 0 | N/A |
| **Total retries** | N/A | 0 | N/A |
| **Avg prompt tokens per scenario** | N/A | 7661 | N/A |
| **Avg completion tokens per scenario** | N/A | 4259 | N/A |
| **Avg tokens per scenario** | N/A | 11921 | N/A |

## Most Frequent Scenario Failures

- **heldout_01**: Expected refund for order 1026 not found
- **heldout_03**: Agent failed to escalate when required
