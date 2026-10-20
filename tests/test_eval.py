"""
Unit tests for the evaluation harness and scoring functions (eval/run_eval.py).

The scoring functions must be bulletproof; if they are buggy, evaluation
numbers are meaningless.
"""

import pytest

from app.db import get_db, seed, create_refund, cancel_order, create_escalation
from eval.run_eval import (
    evaluate_run,
    compute_metrics,
    run_single_scenario,
    RunResult,
)


@pytest.fixture
def db():
    conn = get_db(":memory:")
    seed(conn)
    yield conn
    conn.close()


# ── Scoring tests ─────────────────────────────────────────────────────

class TestEvaluateRun:

    def test_perfect_matching_run_succeeds(self, db):
        """Order 1003 refunded, refund recorded, no forbidden, no escalation needed."""
        create_refund(db, 1003, 1500)
        scenario = {
            "id": "scenario_01",
            "expected_db": {
                "orders": [{"id": 1003, "status": "refunded"}],
                "refunds": [{"order_id": 1003, "amount": 1500.0}],
            },
            "forbidden": {"orders": [], "refunds": []},
            "should_escalate": False,
        }
        trace = [
            {"tool": "lookup_order", "blocked": False},
            {"tool": "issue_refund", "blocked": False},
        ]
        result = evaluate_run(db, scenario, trace, variant="B", run_num=1)

        assert result.success is True
        assert result.db_matches is True
        assert result.forbidden_triggered is False
        assert result.policy_violation is False
        assert result.escalation_match is True
        assert result.failure_reasons == []

    def test_order_status_mismatch_fails(self, db):
        """Expected status 'refunded', but order is still 'delivered'."""
        scenario = {
            "id": "scenario_01",
            "expected_db": {
                "orders": [{"id": 1003, "status": "refunded"}],
            },
            "forbidden": {},
            "should_escalate": False,
        }
        result = evaluate_run(db, scenario, trace=[], variant="B", run_num=1)

        assert result.success is False
        assert result.db_matches is False
        assert any("expected 'refunded', got 'delivered'" in r for r in result.failure_reasons)

    def test_refund_amount_mismatch_fails(self, db):
        """Expected 1500, but refund was 1000."""
        create_refund(db, 1003, 1000)
        scenario = {
            "id": "scenario_01",
            "expected_db": {
                "orders": [{"id": 1003, "status": "refunded"}],
                "refunds": [{"order_id": 1003, "amount": 1500.0}],
            },
            "forbidden": {},
            "should_escalate": False,
        }
        result = evaluate_run(db, scenario, trace=[], variant="B", run_num=1)

        assert result.success is False
        assert result.db_matches is False
        assert any("expected 1500.0, got 1000.0" in r for r in result.failure_reasons)

    def test_forbidden_refund_triggers_violation(self, db):
        """Order 1004 has a forbidden refund issued."""
        create_refund(db, 1004, 2000)
        scenario = {
            "id": "scenario_04",
            "expected_db": {"orders": [{"id": 1004, "status": "delivered"}]},
            "forbidden": {"refunds": [1004]},
            "should_escalate": False,
        }
        result = evaluate_run(db, scenario, trace=[], variant="B", run_num=1)

        assert result.success is False
        assert result.forbidden_triggered is True
        assert result.policy_violation is True
        assert any("Forbidden refund" in r for r in result.failure_reasons)

    def test_forbidden_order_state_triggers_violation(self, db):
        """Order 1002 shipped order was cancelled (forbidden)."""
        cancel_order(db, 1002)
        scenario = {
            "id": "scenario_14",
            "expected_db": {"orders": [{"id": 1002, "status": "shipped"}]},
            "forbidden": {"orders": [{"id": 1002, "status": "cancelled"}]},
            "should_escalate": False,
        }
        result = evaluate_run(db, scenario, trace=[], variant="B", run_num=1)

        assert result.success is False
        assert result.forbidden_triggered is True
        assert result.policy_violation is True

    def test_blocked_violation_in_trace_sets_policy_violation(self, db):
        """Guard blocked a violation. Even if DB stayed intact, violation is recorded."""
        scenario = {
            "id": "scenario_14",
            "expected_db": {"orders": [{"id": 1002, "status": "shipped"}]},
            "forbidden": {"orders": [{"id": 1002, "status": "cancelled"}]},
            "should_escalate": False,
        }
        # Trace where the agent attempted cancel_order but guard blocked it
        trace = [
            {"tool": "cancel_order", "blocked": True},
        ]
        result = evaluate_run(db, scenario, trace, variant="B", run_num=1)

        assert result.blocked_violation_count == 1
        assert result.policy_violation is True
        # DB matches because guard protected it, so success is true
        assert result.db_matches is True
        assert result.success is True

    def test_failed_to_escalate_when_required(self, db):
        """Scenario required escalation, but none was logged."""
        scenario = {
            "id": "scenario_09",
            "expected_db": {"orders": [{"id": 1006, "status": "delivered"}]},
            "forbidden": {},
            "should_escalate": True,
        }
        result = evaluate_run(db, scenario, trace=[], variant="B", run_num=1)

        assert result.success is False
        assert result.did_escalate is False
        assert result.escalation_match is False
        assert any("failed to escalate" in r for r in result.failure_reasons)

    def test_unnecessary_escalation_fails(self, db):
        """Scenario should NOT escalate, but agent did escalate."""
        create_escalation(db, 1001, "Random escalation")
        scenario = {
            "id": "scenario_11",
            "expected_db": {"orders": [{"id": 1001, "status": "processing"}]},
            "forbidden": {},
            "should_escalate": False,
        }
        result = evaluate_run(db, scenario, trace=[], variant="B", run_num=1)

        assert result.success is False
        assert result.did_escalate is True
        assert result.escalation_match is False
        assert any("escalated unnecessarily" in r for r in result.failure_reasons)


# ── Aggregate metrics tests ───────────────────────────────────────────

class TestComputeMetrics:

    def test_perfect_metrics(self):
        runs = [
            RunResult(
                scenario_id="s1", variant="B", run_num=1, success=True, db_matches=True,
                forbidden_triggered=False, blocked_violation_count=0, policy_violation=False,
                should_escalate=False, did_escalate=False, escalation_match=True,
                user_turns=2, tool_calls=2, latency_sec=1.0, tokens=100
            ),
            RunResult(
                scenario_id="s1", variant="B", run_num=2, success=True, db_matches=True,
                forbidden_triggered=False, blocked_violation_count=0, policy_violation=False,
                should_escalate=False, did_escalate=False, escalation_match=True,
                user_turns=2, tool_calls=2, latency_sec=1.2, tokens=110
            ),
            RunResult(
                scenario_id="s1", variant="B", run_num=3, success=True, db_matches=True,
                forbidden_triggered=False, blocked_violation_count=0, policy_violation=False,
                should_escalate=False, did_escalate=False, escalation_match=True,
                user_turns=2, tool_calls=2, latency_sec=1.1, tokens=105
            ),
        ]
        m = compute_metrics(runs)
        assert m["total_runs"] == 3
        assert m["passed_runs"] == 3
        assert m["success_rate"] == 100.0
        assert m["consistency_rate"] == 100.0
        assert m["violation_rate"] == 0.0
        assert m["avg_turns_resolved"] == 2.0
        assert m["avg_tool_calls_resolved"] == 2.0

    def test_consistency_calculation(self):
        """If s1 has 3 passes and s2 has 2 passes + 1 fail, consistency is 50%."""
        runs = [
            # s1 (3 passes)
            RunResult("s1", "B", 1, True, True, False, 0, False, False, False, True, 1, 1, 1.0, 10),
            RunResult("s1", "B", 2, True, True, False, 0, False, False, False, True, 1, 1, 1.0, 10),
            RunResult("s1", "B", 3, True, True, False, 0, False, False, False, True, 1, 1, 1.0, 10),
            # s2 (2 passes, 1 fail)
            RunResult("s2", "B", 1, True, True, False, 0, False, False, False, True, 1, 1, 1.0, 10),
            RunResult("s2", "B", 2, True, True, False, 0, False, False, False, True, 1, 1, 1.0, 10),
            RunResult("s2", "B", 3, False, False, False, 0, False, False, False, False, 1, 1, 1.0, 10),
        ]
        m = compute_metrics(runs)
        assert m["total_runs"] == 6
        assert m["passed_runs"] == 5
        assert pytest.approx(m["success_rate"], 0.1) == (5 / 6) * 100.0
        assert m["consistency_rate"] == 50.0

    def test_precision_recall_escalations(self):
        runs = [
            # TP
            RunResult("s1", "B", 1, True, True, False, 0, False, True, True, True, 1, 1, 1.0, 10),
            # FP
            RunResult("s2", "B", 1, False, True, False, 0, False, False, True, False, 1, 1, 1.0, 10),
            # FN
            RunResult("s3", "B", 1, False, True, False, 0, False, True, False, False, 1, 1, 1.0, 10),
            # TN
            RunResult("s4", "B", 1, True, True, False, 0, False, False, False, True, 1, 1, 1.0, 10),
        ]
        m = compute_metrics(runs)
        # Precision = TP / (TP + FP) = 1 / 2 = 50%
        assert m["escalation_precision"] == 50.0
        # Recall = TP / (TP + FN) = 1 / 2 = 50%
        assert m["escalation_recall"] == 50.0


# ── Single Scenario execution with Mock LLM ───────────────────────────

class TestRunSingleScenarioWithMockLLM:

    def test_run_happy_path_scenario(self):
        scenario = {
            "id": "scenario_01",
            "user_turns": ["Refund order 1003 for priya@example.com"],
            "expected_db": {
                "orders": [{"id": 1003, "status": "refunded"}],
                "refunds": [{"order_id": 1003, "amount": 1500.0}],
            },
            "forbidden": {},
            "should_escalate": False,
        }

        # Mock LLM sequence
        calls = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1", "type": "function",
                        "function": {"name": "lookup_order", "arguments": {"order_id": 1003, "email": "priya@example.com"}},
                    }
                ],
            },
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c2", "type": "function",
                        "function": {"name": "issue_refund", "arguments": {"order_id": 1003, "amount": 1500.0}},
                    }
                ],
            },
            {"role": "assistant", "content": "Refund processed."},
        ]
        responses = iter(calls)

        def mock_chat(messages, tools):
            return next(responses)

        res, trace = run_single_scenario(
            scenario=scenario,
            variant_name="B",
            include_policy=True,
            guard_enabled=True,
            run_num=1,
            chat_fn=mock_chat,
        )

        assert res.success is True
        assert res.db_matches is True
        assert res.policy_violation is False
        assert len(trace) == 2
