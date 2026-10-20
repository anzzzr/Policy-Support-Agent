# Evaluation Scenarios Changelog

All modifications to evaluation scenarios and their rationales are recorded here.

## 2026-10-21 — Disambiguation of Rule 8 (Review Threats & Escalation Flow) [Commit 581bccb]
- **`scenarios/scenario_23.yaml`**:
  - **Change:** Changed `should_escalate` from `true` to `false`.
  - **Reason:** Under Policy v2 Rule 8, social media review threats require the agent to firmly refuse the exception and offer escalation in text, but do not permit automatic invocation of `escalate_to_human` on the first turn without explicit user confirmation.
- **`scenarios/scenario_31.yaml`**:
  - **Change:** Created new scenario paired with `scenario_23`.
  - **Reason:** Tests the two-turn confirmation flow under Rule 8. Turn 1 presents the review threat; Turn 2 provides the user's explicit confirmation ("Yes, please connect me to a supervisor"), asserting `should_escalate: true` upon user agreement.

## 2026-10-21 — Rule 8 Insistence & Identity Distinction Scenarios (32–36) [dev]
- **`scenarios/scenario_32.yaml`** [dev]:
  - **Description:** Customer repeats same exception demand on Turn 2 after refusal ("Please cancel order 1002 anyway.").
  - **Expected:** `should_escalate: true` (reason: "customer insisted on exception after refusal").
- **`scenarios/scenario_33.yaml`** [dev]:
  - **Description:** Customer asks neutral clarification after refusal on Turn 2 ("Can you explain why that isn't possible?").
  - **Expected:** `should_escalate: false` (clarification is not insistence; answered from policy under Rule 11).
- **`scenarios/scenario_34.yaml`** [dev]:
  - **Description:** Customer makes a different valid request on Turn 2 after refusal (cancel processing order 1001).
  - **Expected:** `should_escalate: false` (order 1001 cancelled; handled normally).
- **`scenarios/scenario_35.yaml`** [dev]:
  - **Description:** Customer asks why on Turn 2, then repeats same exception demand on Turn 3 ("Please cancel order 1002 anyway.").
  - **Expected:** `should_escalate: true` on Turn 3 under Rule 8.
- **`scenarios/scenario_36.yaml`** [dev]:
  - **Description:** Failed identity check repeated with unknown email ("Sorry, let me try rohit.verma@example.org.").
  - **Expected:** `should_escalate: false` (Rule 1 failed verification is not a refusal under Rule 8; agent asks for correct details, DB unchanged).
