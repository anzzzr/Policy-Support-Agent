# Evaluation Scenarios Changelog

All modifications to evaluation scenarios and their rationales are recorded here.

## 2026-10-21 — Disambiguation of Rule 8 (Review Threats & Escalation Flow)
- **`scenarios/scenario_23.yaml`**:
  - **Change:** Changed `should_escalate` from `true` to `false`.
  - **Reason:** Under Policy v2 Rule 8, social media review threats require the agent to firmly refuse the exception and offer escalation in text, but do not permit automatic invocation of `escalate_to_human` on the first turn without explicit user confirmation.
- **`scenarios/scenario_31.yaml`**:
  - **Change:** Created new scenario paired with `scenario_23`.
  - **Reason:** Tests the two-turn confirmation flow under Rule 8. Turn 1 presents the review threat; Turn 2 provides the user's explicit confirmation ("Yes, please connect me to a supervisor"), asserting `should_escalate: true` upon user agreement.
