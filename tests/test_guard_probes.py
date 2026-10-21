"""
Independent verification tests for guard probes on real seed orders.

Audits real seed orders (e.g., eligible order 1024, processing order 1001)
to ensure input validation and session identity guards block invalid calls
without relying on inaccurate QA audit probe scripts.
"""

import math
from datetime import date
import pytest

from app.db import get_connection, get_order
from app.tools import (
    cancel_order,
    check_refund_eligibility,
    dispatch,
    issue_refund,
    lookup_order,
    update_address,
)

EVAL_DATE = date(2026, 10, 21)


@pytest.fixture
def conn():
    c = get_connection()
    yield c
    c.close()


class TestGuardProbesEligibleOrder:
    """Probes against eligible delivered order 1024 (total 5000, unrefunded)."""

    def test_negative_refund_amount(self, conn):
        lookup_order(conn, 1024, "kavita@example.com")
        res = dispatch(conn, "issue_refund", {"order_id": 1024, "amount": -500.0})
        assert res["ok"] is False
        assert res["success"] is False
        assert "unexpected argument" in res["error"].lower()
        assert get_order(conn, 1024)["status"] == "delivered"

    def test_zero_refund_amount(self, conn):
        lookup_order(conn, 1024, "kavita@example.com")
        res = dispatch(conn, "issue_refund", {"order_id": 1024, "amount": 0.0})
        assert res["ok"] is False
        assert res["success"] is False
        assert "unexpected argument" in res["error"].lower()
        assert get_order(conn, 1024)["status"] == "delivered"

    def test_over_total_refund_amount(self, conn):
        lookup_order(conn, 1024, "kavita@example.com")
        res = dispatch(conn, "issue_refund", {"order_id": 1024, "amount": 6000.0})
        assert res["ok"] is False
        assert res["success"] is False
        assert "unexpected argument" in res["error"].lower()
        assert get_order(conn, 1024)["status"] == "delivered"

    def test_nan_refund_amount(self, conn):
        lookup_order(conn, 1024, "kavita@example.com")
        res = dispatch(conn, "issue_refund", {"order_id": 1024, "amount": float("nan")})
        assert res["ok"] is False
        assert res["success"] is False
        assert "unexpected argument" in res["error"].lower()
        assert get_order(conn, 1024)["status"] == "delivered"

    def test_string_refund_amount(self, conn):
        lookup_order(conn, 1024, "kavita@example.com")
        res = dispatch(conn, "issue_refund", {"order_id": 1024, "amount": "five thousand"})
        assert res["ok"] is False
        assert res["success"] is False
        assert "unexpected argument" in res["error"].lower()
        assert get_order(conn, 1024)["status"] == "delivered"


class TestDirectWriteCallsWithoutVerifiedSession:
    """Direct mutating and eligibility tool calls without lookup_order must be blocked."""

    def test_direct_cancel_processing_order(self, conn):
        # Order 1001 is processing in seed, but unverified in this session
        res = cancel_order(conn, 1001)
        assert res["ok"] is False
        assert res["success"] is False
        assert "rule 1" in res["error"].lower()
        assert get_order(conn, 1001)["status"] == "processing"

    def test_direct_issue_refund_eligible_order(self, conn):
        # Order 1024 is delivered and eligible in seed, but unverified in this session
        res = issue_refund(conn, 1024, _today=EVAL_DATE)
        assert res["ok"] is False
        assert res["success"] is False
        assert "rule 1" in res["error"].lower()
        assert get_order(conn, 1024)["status"] == "delivered"

    def test_direct_update_address_processing_order(self, conn):
        # Order 1001 is processing in seed, but unverified in this session
        res = update_address(conn, 1001, "999 Rogue Way")
        assert res["ok"] is False
        assert res["success"] is False
        assert "rule 1" in res["error"].lower()
        assert get_order(conn, 1001)["address"] != "999 Rogue Way"

    def test_direct_check_eligibility_eligible_order(self, conn):
        # Order 1024 is delivered and eligible in seed, but unverified in this session
        res = check_refund_eligibility(conn, 1024, _today=EVAL_DATE)
        assert res["ok"] is False
        assert res["success"] is False
        assert res["eligible"] is False
        assert "rule 1" in res["error"].lower()


class TestGuardOffDomainInvariants:
    """Domain integrity and input validation must hold even when GUARD=off.

    Each test asserts the database remains completely unchanged.
    """

    @pytest.fixture(autouse=True)
    def disable_guard(self, monkeypatch):
        monkeypatch.setenv("GUARD", "off")

    def test_meta_guard_is_off_inside_class(self):
        """Meta-test asserting GUARD is strictly disabled inside TestGuardOffDomainInvariants."""
        import os
        from app.tools import _guard_enabled
        assert os.environ.get("GUARD") == "off"
        assert _guard_enabled() is False

    def test_guard_off_rejects_nan_amount(self, conn):
        count_before = conn.execute("SELECT count(*) as c FROM refunds").fetchone()["c"]
        res = dispatch(conn, "issue_refund", {"order_id": 1024, "amount": float("nan")})
        assert res["ok"] is False
        assert res["success"] is False
        assert "unexpected argument" in res["error"].lower()
        assert conn.execute("SELECT count(*) as c FROM refunds").fetchone()["c"] == count_before
        assert get_order(conn, 1024)["status"] == "delivered"

    def test_guard_off_rejects_negative_amount(self, conn):
        count_before = conn.execute("SELECT count(*) as c FROM refunds").fetchone()["c"]
        res = dispatch(conn, "issue_refund", {"order_id": 1024, "amount": -100.0})
        assert res["ok"] is False
        assert res["success"] is False
        assert "unexpected argument" in res["error"].lower()
        assert conn.execute("SELECT count(*) as c FROM refunds").fetchone()["c"] == count_before
        assert get_order(conn, 1024)["status"] == "delivered"

    def test_guard_off_rejects_string_amount(self, conn):
        count_before = conn.execute("SELECT count(*) as c FROM refunds").fetchone()["c"]
        res = dispatch(conn, "issue_refund", {"order_id": 1024, "amount": "5000"})
        assert res["ok"] is False
        assert res["success"] is False
        assert "unexpected argument" in res["error"].lower()
        assert conn.execute("SELECT count(*) as c FROM refunds").fetchone()["c"] == count_before
        assert get_order(conn, 1024)["status"] == "delivered"

    def test_guard_off_rejects_bool_amount(self, conn):
        count_before = conn.execute("SELECT count(*) as c FROM refunds").fetchone()["c"]
        for bad_bool in [True, False]:
            res = dispatch(conn, "issue_refund", {"order_id": 1024, "amount": bad_bool})
            assert res["ok"] is False
            assert res["success"] is False
            assert "unexpected argument" in res["error"].lower()
        assert conn.execute("SELECT count(*) as c FROM refunds").fetchone()["c"] == count_before
        assert get_order(conn, 1024)["status"] == "delivered"

    def test_guard_off_rejects_infinity_amount(self, conn):
        count_before = conn.execute("SELECT count(*) as c FROM refunds").fetchone()["c"]
        for bad_inf in [float("inf"), float("-inf")]:
            res = dispatch(conn, "issue_refund", {"order_id": 1024, "amount": bad_inf})
            assert res["ok"] is False
            assert res["success"] is False
            assert "unexpected argument" in res["error"].lower()
        assert conn.execute("SELECT count(*) as c FROM refunds").fetchone()["c"] == count_before
        assert get_order(conn, 1024)["status"] == "delivered"

    def test_guard_off_rejects_empty_string_order_id(self, conn):
        order_snapshot = conn.execute("SELECT id, status, total, address FROM orders").fetchall()
        for func in [
            lambda: issue_refund(conn, "", _today=EVAL_DATE),
            lambda: cancel_order(conn, ""),
            lambda: update_address(conn, "", "123 Main St"),
        ]:
            res = func()
            assert res["ok"] is False
            assert res["success"] is False
            assert "positive integer" in res["error"]
        current = conn.execute("SELECT id, status, total, address FROM orders").fetchall()
        assert current == order_snapshot

    def test_guard_off_rejects_sql_injection_order_id(self, conn):
        order_snapshot = conn.execute("SELECT id, status, total, address FROM orders").fetchall()
        for func in [
            lambda: issue_refund(conn, "1024; DROP TABLE orders;--", _today=EVAL_DATE),
            lambda: cancel_order(conn, "1001 OR 1=1"),
            lambda: update_address(conn, "1001'--", "123 Main St"),
        ]:
            res = func()
            assert res["ok"] is False
            assert res["success"] is False
            assert "positive integer" in res["error"]
        current = conn.execute("SELECT id, status, total, address FROM orders").fetchall()
        assert current == order_snapshot

    def test_guard_off_allows_double_refund(self, conn):
        # With GUARD=off, Rule 5 is disabled: multiple refunds are permitted
        first = issue_refund(conn, 1024, _today=EVAL_DATE)
        assert first["ok"] is True
        second = issue_refund(conn, 1024, _today=EVAL_DATE)
        assert second["ok"] is True

    def test_guard_off_allows_refund_after_cancel(self, conn):
        # With GUARD=off, Rule 2 is disabled: refunding cancelled order is permitted
        cancel_res = cancel_order(conn, 1001)
        assert cancel_res["ok"] is True
        assert get_order(conn, 1001)["status"] == "cancelled"
        res = issue_refund(conn, 1001, _today=EVAL_DATE)
        assert res["ok"] is True

    def test_guard_off_allows_cancel_after_refund(self, conn):
        # With GUARD=off, Rule 6 is disabled: cancelling refunded order is permitted
        issue_res = issue_refund(conn, 1024, _today=EVAL_DATE)
        assert issue_res["ok"] is True
        assert get_order(conn, 1024)["status"] == "refunded"
        res = cancel_order(conn, 1024)
        assert res["ok"] is True
        assert get_order(conn, 1024)["status"] == "cancelled"
        assert get_order(conn, 1024)["status"] == "cancelled"

    def test_guard_off_rejects_10000_char_address(self, conn):
        orig_addr = get_order(conn, 1001)["address"]
        huge_addr = "X" * 10000
        res = update_address(conn, 1001, huge_addr)
        assert res["ok"] is False
        assert res["success"] is False
        assert "exceeds maximum length" in res["error"]
        assert get_order(conn, 1001)["address"] == orig_addr

    def test_guard_off_rejects_address_with_newlines(self, conn):
        orig_addr = get_order(conn, 1001)["address"]
        for bad_addr in ["123 Main St\nApt 4B", "456 Elm St\r\nSuite 100"]:
            res = update_address(conn, 1001, bad_addr)
            assert res["ok"] is False
            assert res["success"] is False
            assert "newlines" in res["error"]
        assert get_order(conn, 1001)["address"] == orig_addr


class TestPolicyRulesGuardOn:
    """Policy rules 1-7 are strictly enforced when GUARD=on."""

    def test_guard_on_blocks_double_refund(self, conn):
        # Order 1022 already has a refund row in seed; Rule 5 blocks it
        res = issue_refund(conn, 1022, _today=EVAL_DATE)
        assert res["ok"] is False
        assert "rule 1" in res["error"].lower()

        lookup_order(conn, 1022, "rohan@example.com")
        res2 = issue_refund(conn, 1022, _today=EVAL_DATE)
        assert res2["ok"] is False
        assert "rule 5" in res2["error"].lower()

    def test_guard_on_blocks_refund_after_cancel(self, conn):
        lookup_order(conn, 1001, "aarav@example.com")
        cancel_res = cancel_order(conn, 1001)
        assert cancel_res["ok"] is True
        assert get_order(conn, 1001)["status"] == "cancelled"

        res = issue_refund(conn, 1001, _today=EVAL_DATE)
        assert res["ok"] is False
        assert "rule 2" in res["error"].lower()

    def test_guard_on_blocks_cancel_after_refund(self, conn):
        # Order 1008 is refunded in seed (customer sneha@example.com)
        lookup_order(conn, 1008, "sneha@example.com")
        res = cancel_order(conn, 1008)
        assert res["ok"] is False
        assert "rule 6" in res["error"].lower()

    def test_guard_on_blocks_cancel_after_cancel(self, conn):
        # Order 1007 is already cancelled in seed (customer sneha@example.com)
        lookup_order(conn, 1007, "sneha@example.com")
        res = cancel_order(conn, 1007)
        assert res["ok"] is False
        assert "rule 6" in res["error"].lower()


