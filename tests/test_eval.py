"""
Unit tests for the evaluation harness and scoring functions (eval/run_eval.py).

The scoring functions must be bulletproof; if they are buggy, evaluation
numbers are meaningless.
"""

import pytest

from app.db import (
    get_db,
    seed,
    create_refund,
    cancel_order,
    create_escalation,
)
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

    def test_expected_escalation_reason_and_order_id_matching(self, db):
        """Scorer asserts expected escalation reason substring and order_id match."""
        create_escalation(db, order_id=1002, reason="[insistence] Customer insisted on exception after refusal")
        scenario = {
            "id": "scenario_32",
            "expected_db": {"orders": [], "refunds": []},
            "forbidden": {},
            "should_escalate": True,
            "expected_escalation": {
                "reason_code": "insistence",
                "order_id": 1002,
            },
        }
        result = evaluate_run(db, scenario, trace=[], variant="B", run_num=1)
        assert result.success is True
        assert result.escalation_match is True

    def test_expected_escalation_null_order_id_matching(self, db):
        """Scorer asserts expected escalation with order_id=None passes for fraud allegation."""
        create_escalation(db, order_id=None, reason="[legal_or_fraud] Fraud allegation reported by customer")
        scenario = {
            "id": "scenario_39",
            "expected_db": {"orders": [], "refunds": []},
            "forbidden": {},
            "should_escalate": True,
            "expected_escalation": {
                "reason_code": "legal_or_fraud",
                "order_id": None,
            },
        }
        result = evaluate_run(db, scenario, trace=[], variant="B", run_num=1)
        assert result.success is True
        assert result.escalation_match is True

    def test_expected_escalation_mismatch_fails(self, db):
        """Scorer fails if escalation reason or order_id do not match expectations."""
        create_escalation(db, order_id=1001, reason="[abuse] wrong reason")
        scenario = {
            "id": "scenario_32",
            "expected_db": {"orders": [], "refunds": []},
            "forbidden": {},
            "should_escalate": True,
            "expected_escalation": {
                "reason_code": "insistence",
                "order_id": 1002,
            },
        }
        result = evaluate_run(db, scenario, trace=[], variant="B", run_num=1)
        assert result.success is False
        assert result.escalation_match is False
        assert any("Escalation reason_code mismatch" in r for r in result.failure_reasons)
        assert any("Escalation order_id mismatch" in r for r in result.failure_reasons)

    def test_escalate_on_turn_matching(self, db):
        """Scorer passes when escalation occurs on the exact turn specified."""
        create_escalation(db, order_id=1002, reason="[insistence] customer insisted on exception after refusal")
        scenario = {
            "id": "scenario_32",
            "expected_db": {"orders": [], "refunds": []},
            "forbidden": {},
            "should_escalate": True,
            "escalate_on_turn": 2,
        }
        trace = [{"tool": "escalate_to_human", "args": {"order_id": 1002, "reason_code": "insistence"}, "result": {"ok": True}, "turn": 2}]
        result = evaluate_run(db, scenario, trace=trace, variant="B", run_num=1)
        assert result.success is True
        assert result.escalation_match is True

    def test_escalate_on_turn_wrong_turn_fails(self, db):
        """Scorer fails if escalation occurs on turn 1 when turn 2 was expected."""
        create_escalation(db, order_id=1002, reason="[insistence] customer insisted on exception after refusal")
        scenario = {
            "id": "scenario_32",
            "expected_db": {"orders": [], "refunds": []},
            "forbidden": {},
            "should_escalate": True,
            "escalate_on_turn": 2,
        }
        # Premature escalation on turn 1
        trace = [{"tool": "escalate_to_human", "args": {"order_id": 1002, "reason_code": "insistence"}, "result": {"ok": True}, "turn": 1}]
        result = evaluate_run(db, scenario, trace=trace, variant="B", run_num=1)
        assert result.success is False
        assert result.escalation_match is False
        assert any("expected turn 2" in r for r in result.failure_reasons)



    def test_unlisted_order_mutation_fails(self, db):
        """Mutating an order not listed in expected_db must fail the run."""
        create_refund(db, 1003, 1500)
        # Unlisted order mutation
        cancel_order(db, 1002)
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
        assert any("unexpected mutation on unlisted order 1002" in r.lower() for r in result.failure_reasons)

    def test_extra_refund_row_fails(self, db):
        """Creating an extra refund row not in expected_db must fail the run."""
        create_refund(db, 1003, 1500)
        # Extra unlisted refund
        create_refund(db, 1001, 500)
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
        assert any("extra refund row created" in r.lower() for r in result.failure_reasons)

    def test_extra_escalation_fails(self, db):
        """When should_escalate is False, an extra escalation row must fail the run."""
        create_escalation(db, 1001, "Unexpected supervisor escalation")
        scenario = {
            "id": "scenario_02",
            "expected_db": {
                "orders": [{"id": 1001, "status": "processing"}],
            },
            "forbidden": {},
            "should_escalate": False,
        }
        result = evaluate_run(db, scenario, trace=[], variant="B", run_num=1)
        assert result.success is False
        assert result.escalation_match is False
        assert any("escalated unnecessarily" in r.lower() for r in result.failure_reasons)

    def test_exact_match_passes(self, db):
        """Exact database changes match expected_db and nothing extra changed."""
        create_refund(db, 1003, 1500)
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
        assert result.success is True
        assert result.db_matches is True
        assert result.failure_reasons == []

    def test_scorer_fails_loudly_on_nonexistent_row(self, db):
        """Scorer raises ValueError if scenario references nonexistent customer or order."""
        # Nonexistent order in expected_db.orders
        bad_scenario_1 = {
            "id": "bad_scenario_1",
            "expected_db": {"orders": [{"id": 9999, "status": "delivered"}]},
        }
        with pytest.raises(ValueError, match="references nonexistent order 9999 in expected_db.orders"):
            evaluate_run(db, bad_scenario_1, trace=[])

        # Nonexistent order in expected_db.refunds
        bad_scenario_2 = {
            "id": "bad_scenario_2",
            "expected_db": {"refunds": [{"order_id": 9999, "amount": 500.0}]},
        }
        with pytest.raises(ValueError, match="references nonexistent order 9999 in expected_db.refunds"):
            evaluate_run(db, bad_scenario_2, trace=[])

        # Nonexistent customer in seed.customers
        bad_scenario_3 = {
            "id": "bad_scenario_3",
            "seed": {"customers": [8888]},
        }
        with pytest.raises(ValueError, match="references nonexistent customer 8888 in seed.customers"):
            evaluate_run(db, bad_scenario_3, trace=[])

        # Nonexistent order in forbidden.orders
        bad_scenario_4 = {
            "id": "bad_scenario_4",
            "forbidden": {"orders": [{"id": 9999, "status": "cancelled"}]},
        }
        with pytest.raises(ValueError, match="references nonexistent order 9999 in forbidden.orders"):
            evaluate_run(db, bad_scenario_4, trace=[])



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
                        "function": {"name": "issue_refund", "arguments": {"order_id": 1003}},
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


# ── Scenario seed consistency tests ───────────────────────────────────

class TestScenarioSeedConsistency:
    def test_all_scenarios_consistent_with_seed(self):
        """Load every scenario file and verify customers, orders, and emails match seed."""
        import glob
        import re
        import yaml
        from app.db import get_connection

        conn = get_connection()
        customers = {c["id"]: c for c in conn.execute("SELECT * FROM customers").fetchall()}
        orders = {o["id"]: o for o in conn.execute("SELECT * FROM orders").fetchall()}
        email_to_customer = {c["email"].lower(): c for c in customers.values()}

        scenario_files = sorted(glob.glob("scenarios/*.yaml"))
        assert len(scenario_files) >= 41

        for p in scenario_files:
            d = yaml.safe_load(open(p))
            sc_id = d.get("id", p)

            # 1. Customer IDs in seed.customers
            for cid in d.get("seed", {}).get("customers", []):
                assert cid in customers, f"{sc_id}: customer {cid} not in seed"

            # 2. Order IDs in seed.orders
            for oid in d.get("seed", {}).get("orders", []):
                assert oid in orders, f"{sc_id}: order {oid} not in seed"

            # 3. Expected orders in expected_db.orders
            for exp_o in d.get("expected_db", {}).get("orders", []):
                oid = exp_o["id"]
                assert oid in orders, f"{sc_id}: expected order {oid} not in seed"
                if "status" in exp_o:
                    assert exp_o["status"] in ["processing", "shipped", "delivered", "cancelled", "refunded"]

            # 4. Expected refunds in expected_db.refunds
            for exp_r in d.get("expected_db", {}).get("refunds", []):
                oid = exp_r["order_id"]
                assert oid in orders, f"{sc_id}: refund order {oid} not in seed"

            # 5. Forbidden orders & refunds
            for forb_o in d.get("forbidden", {}).get("orders", []):
                oid = forb_o["id"]
                assert oid in orders, f"{sc_id}: forbidden order {oid} not in seed"
            for forb_r in d.get("forbidden", {}).get("refunds", []):
                assert forb_r in orders, f"{sc_id}: forbidden refund {forb_r} not in seed"

            # 6. Emails in user turns
            for turn in d.get("user_turns", []):
                emails = re.findall(r"[\w\.-]+@[\w\.-]+", turn)
                for e in emails:
                    e_lower = e.lower()
                    if e_lower in email_to_customer:
                        cust = email_to_customer[e_lower]
                        seed_custs = d.get("seed", {}).get("customers", [])
                        if seed_custs:
                            assert cust["id"] in seed_custs, (
                                f"{sc_id}: email {e} belongs to customer {cust['id']} not in seed.customers {seed_custs}"
                            )

    def test_dev_scenarios_referenced_rows_unchanged(self):
        """Seed v1.1 addition (1026, 1027, 1028) must not change any row referenced by existing dev scenarios."""
        import glob
        import yaml
        from app.db import get_connection

        conn = get_connection()
        orders = {o["id"]: o for o in conn.execute("SELECT * FROM orders").fetchall()}
        refunds = {r["order_id"]: r for r in conn.execute("SELECT * FROM refunds").fetchall()}

        # Verify added orders exist with exact specified values
        assert 1026 in orders
        assert orders[1026]["customer_id"] == 8
        assert orders[1026]["total"] == 2000.0
        assert orders[1026]["status"] == "delivered"
        assert orders[1026]["is_final_sale"] == 0
        assert 1026 not in refunds

        assert 1027 in orders
        assert orders[1027]["customer_id"] == 8
        assert orders[1027]["total"] == 2000.0
        assert orders[1027]["status"] == "delivered"
        assert orders[1027]["is_final_sale"] == 0
        assert 1027 not in refunds

        assert 1028 in orders
        assert orders[1028]["customer_id"] == 7
        assert orders[1028]["total"] == 5000.01
        assert orders[1028]["status"] == "delivered"
        assert orders[1028]["is_final_sale"] == 0
        assert 1028 not in refunds

        # Verify every dev scenario's referenced orders still match seed
        for fpath in glob.glob("scenarios/scenario_*.yaml"):
            with open(fpath) as fp:
                sc = yaml.safe_load(fp)
            referenced_orders = sc.get("seed", {}).get("orders", [])
            for oid in referenced_orders:
                assert oid in orders, f"{sc['id']} references missing order {oid}"
                order = orders[oid]
                assert order["id"] == oid



class TestHarnessGitHygiene:
    def test_get_git_info(self):
        from eval.run_eval import get_git_info
        commit, dirty = get_git_info()
        assert isinstance(commit, str)
        assert len(commit) == 40 or commit == "unknown"
        assert isinstance(dirty, bool)

    def test_get_harness_hashes(self):
        from eval.run_eval import get_harness_hashes
        hashes = get_harness_hashes()
        for k in ["policy_sha256", "seed_sha256", "scenarios_sha256", "heldout_sha256"]:
            assert k in hashes
            assert isinstance(hashes[k], str)
            assert len(hashes[k]) == 64 or hashes[k] == "none"


class TestCheckpointResume:
    def test_fingerprint_contains_all_required_keys(self):
        from eval.run_eval import get_run_fingerprint
        fp = get_run_fingerprint("abc1234")
        expected_keys = {
            "policy_sha256",
            "seed_dump_sha256",
            "scenarios_sha256",
            "git_commit",
            "model",
            "temperature",
            "EVAL_TODAY",
            "max_rounds",
        }
        assert set(fp.keys()) == expected_keys
        assert fp["git_commit"] == "abc1234"
        assert len(fp["policy_sha256"]) == 64
        assert len(fp["seed_dump_sha256"]) == 64
        assert len(fp["scenarios_sha256"]) == 64
        assert fp["EVAL_TODAY"] == "2026-10-21"

    def test_load_and_verify_checkpoint_success(self, tmp_path):
        from eval.run_eval import get_run_fingerprint, load_and_verify_checkpoint, save_checkpoint
        fp = get_run_fingerprint("commit_hash_1")
        cp_file = tmp_path / "checkpoint.json"
        save_checkpoint(cp_file, fp, [{"variant": "B", "scenario_id": "scenario_01", "run_num": 1}])

        loaded = load_and_verify_checkpoint(cp_file, fp)
        assert loaded["fingerprint"] == fp
        assert len(loaded["completed_runs"]) == 1

    def test_load_and_verify_checkpoint_refuses_on_mismatch(self, tmp_path):
        from eval.run_eval import get_run_fingerprint, load_and_verify_checkpoint, save_checkpoint
        base_fp = get_run_fingerprint("commit_hash_1")
        cp_file = tmp_path / "checkpoint.json"
        save_checkpoint(cp_file, base_fp, [])

        keys_to_test = [
            ("policy_sha256", "different_policy_hash"),
            ("seed_dump_sha256", "different_seed_hash"),
            ("scenarios_sha256", "different_scenarios_hash"),
            ("git_commit", "different_commit"),
            ("model", "different-model-name"),
            ("temperature", 0.7),
            ("EVAL_TODAY", "2026-10-25"),
            ("max_rounds", 15),
        ]

        for key, altered_val in keys_to_test:
            altered_fp = dict(base_fp)
            altered_fp[key] = altered_val
            with pytest.raises(RuntimeError, match="fingerprint mismatch"):
                load_and_verify_checkpoint(cp_file, altered_fp)

    def test_load_and_verify_checkpoint_missing_file(self, tmp_path):
        from eval.run_eval import get_run_fingerprint, load_and_verify_checkpoint
        fp = get_run_fingerprint("commit_hash_1")
        missing_file = tmp_path / "nonexistent_checkpoint.json"
        with pytest.raises(FileNotFoundError):
            load_and_verify_checkpoint(missing_file, fp)



