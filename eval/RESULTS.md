# Evaluation Results

Evaluation results across 30 test scenarios (3 runs per scenario = 90 runs per variant).
Quality is measured by the **final database state**, not conversation style.

## Variants Comparison

| Metric | (A) No Policy in Prompt, Guard ON | (B) Policy in Prompt, Guard ON | (C) Policy in Prompt, Guard OFF |
|---|---|---|---|
| **Final-state success rate** | N/A | 100.0% | N/A |
| **Consistency (all 3 runs passed)** | N/A | 100.0% | N/A |
| **Policy-violation rate** | N/A | 0.0% | N/A |
| **Escalation Precision** | N/A | 100.0% | N/A |
| **Escalation Recall** | N/A | 100.0% | N/A |
| **Avg turns per resolved scenario** | N/A | 2.0 | N/A |
| **Avg tool calls per resolved scenario** | N/A | 3.0 | N/A |
| **Avg latency per scenario** | N/As | 3.43s | N/As |
| **Avg tokens per scenario** | N/A | 6021 | N/A |

## Most Frequent Scenario Failures

_No failures recorded across evaluation runs._
