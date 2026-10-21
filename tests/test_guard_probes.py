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
        res = issue_refund(conn, 1024, -500.0, _today=EVAL_DATE)
        assert res["ok"] is False
        assert res["success"] is False
        assert "greater than zero" in res["error"]
        assert get_order(conn, 1024)["status"] == "delivered"

    def test_zero_refund_amount(self, conn):
        lookup_order(conn, 1024, "kavita@example.com")
        res = issue_refund(conn, 1024, 0.0, _today=EVAL_DATE)
        assert res["ok"] is False
        assert res["success"] is False
        assert "greater than zero" in res["error"]
        assert get_order(conn, 1024)["status"] == "delivered"

    def test_over_total_refund_amount(self, conn):
        lookup_order(conn, 1024, "kavita@example.com")
        res = issue_refund(conn, 1024, 6000.0, _today=EVAL_DATE)
        assert res["ok"] is False
        assert res["success"] is False
        assert "exceeds order total" in res["error"]
        assert get_order(conn, 1024)["status"] == "delivered"

    def test_nan_refund_amount(self, conn):
        lookup_order(conn, 1024, "kavita@example.com")
        res = issue_refund(conn, 1024, float("nan"), _today=EVAL_DATE)
        assert res["ok"] is False
        assert res["success"] is False
        assert "finite number" in res["error"]
        assert get_order(conn, 1024)["status"] == "delivered"

    def test_string_refund_amount(self, conn):
        lookup_order(conn, 1024, "kavita@example.com")
        res = issue_refund(conn, 1024, "five thousand", _today=EVAL_DATE)
        assert res["ok"] is False
        assert res["success"] is False
        assert "finite number" in res["error"]
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
        res = issue_refund(conn, 1024, 5000.0, _today=EVAL_DATE)
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
        res = issue_refund(conn, 1024, float("nan"), _today=EVAL_DATE)
        assert res["ok"] is False
        assert res["success"] is False
        assert "finite number" in res["error"]
        assert conn.execute("SELECT count(*) as c FROM refunds").fetchone()["c"] == count_before
        assert get_order(conn, 1024)["status"] == "delivered"

    def test_guard_off_rejects_negative_amount(self, conn):
        count_before = conn.execute("SELECT count(*) as c FROM refunds").fetchone()["c"]
        res = issue_refund(conn, 1024, -100.0, _today=EVAL_DATE)
        assert res["ok"] is False
        assert res["success"] is False
        assert "greater than zero" in res["error"]
        assert conn.execute("SELECT count(*) as c FROM refunds").fetchone()["c"] == count_before
        assert get_order(conn, 1024)["status"] == "delivered"

    def test_guard_off_rejects_string_amount(self, conn):
        count_before = conn.execute("SELECT count(*) as c FROM refunds").fetchone()["c"]
        res = issue_refund(conn, 1024, "5000", _today=EVAL_DATE)
        assert res["ok"] is False
        assert res["success"] is False
        assert "finite number" in res["error"]
        assert conn.execute("SELECT count(*) as c FROM refunds").fetchone()["c"] == count_before
        assert get_order(conn, 1024)["status"] == "delivered"

    def test_guard_off_rejects_bool_amount(self, conn):
        count_before = conn.execute("SELECT count(*) as c FROM refunds").fetchone()["c"]
        for bad_bool in [True, False]:
            res = issue_refund(conn, 1024, bad_bool, _today=EVAL_DATE)
            assert res["ok"] is False
            assert res["success"] is False
            assert "finite number" in res["error"]
        assert conn.execute("SELECT count(*) as c FROM refunds").fetchone()["c"] == count_before
        assert get_order(conn, 1024)["status"] == "delivered"

    def test_guard_off_rejects_infinity_amount(self, conn):
        count_before = conn.execute("SELECT count(*) as c FROM refunds").fetchone()["c"]
        for bad_inf in [float("inf"), float("-inf")]:
            res = issue_refund(conn, 1024, bad_inf, _today=EVAL_DATE)
            assert res["ok"] is False
            assert res["success"] is False
            assert "finite number" in res["error"]
        assert conn.execute("SELECT count(*) as c FROM refunds").fetchone()["c"] == count_before
        assert get_order(conn, 1024)["status"] == "delivered"

    def test_guard_off_rejects_empty_string_order_id(self, conn):
        order_snapshot = conn.execute("SELECT id, status, total, address FROM orders").fetchall()
        for func in [
            lambda: issue_refund(conn, "", 5000.0, _today=EVAL_DATE),
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
            lambda: issue_refund(conn, "1024; DROP TABLE orders;--", 5000.0, _today=EVAL_DATE),
            lambda: cancel_order(conn, "1001 OR 1=1"),
            lambda: update_address(conn, "1001'--", "123 Main St"),
        ]:
            res = func()
            assert res["ok"] is False
            assert res["success"] is False
            assert "positive integer" in res["error"]
        current = conn.execute("SELECT id, status, total, address FROM orders").fetchall()
        assert current == order_snapshot

    def test_guard_off_rejects_double_refund(self, conn):
        # 1. Order 1022 already has a refund row in seed
        count_before = conn.execute("SELECT count(*) as c FROM refunds").fetchone()["c"]
        res = issue_refund(conn, 1022, 1200.0, _today=EVAL_DATE)
        assert res["ok"] is False
        assert res["success"] is False
        assert "already refunded" in res["error"].lower()
        assert conn.execute("SELECT count(*) as c FROM refunds").fetchone()["c"] == count_before

        # 2. Order 1024 refunded once, then second refund attempt rejected
        first = issue_refund(conn, 1024, 5000.0, _today=EVAL_DATE)
        assert first["ok"] is True
        assert get_order(conn, 1024)["status"] == "refunded"
        count_after_first = conn.execute("SELECT count(*) as c FROM refunds").fetchone()["c"]
        second = issue_refund(conn, 1024, 5000.0, _today=EVAL_DATE)
        assert second["ok"] is False
        assert second["success"] is False
        assert "already refunded" in second["error"].lower()
        assert conn.execute("SELECT count(*) as c FROM refunds").fetchone()["c"] == count_after_first
        assert get_order(conn, 1024)["status"] == "refunded"

    def test_guard_off_rejects_refund_after_cancel(self, conn):
        # Cancel order 1001 first
        cancel_res = cancel_order(conn, 1001)
        assert cancel_res["ok"] is True
        assert get_order(conn, 1001)["status"] == "cancelled"

        count_before = conn.execute("SELECT count(*) as c FROM refunds").fetchone()["c"]
        res = issue_refund(conn, 1001, 2500.0, _today=EVAL_DATE)
        assert res["ok"] is False
        assert res["success"] is False
        assert "cancelled" in res["error"].lower()
        assert conn.execute("SELECT count(*) as c FROM refunds").fetchone()["c"] == count_before
        assert get_order(conn, 1001)["status"] == "cancelled"

    def test_guard_off_rejects_cancel_after_refund(self, conn):
        # Order 1022 already has a refund in seed
        res1 = cancel_order(conn, 1022)
        assert res1["ok"] is False
        assert res1["success"] is False
        assert "refunded" in res1["error"].lower()

        # Order 1024 refunded via issue_refund, then cancel rejected
        issue_res = issue_refund(conn, 1024, 5000.0, _today=EVAL_DATE)
        assert issue_res["ok"] is True
        assert get_order(conn, 1024)["status"] == "refunded"

        res2 = cancel_order(conn, 1024)
        assert res2["ok"] is False
        assert res2["success"] is False
        assert "refunded" in res2["error"].lower()
        assert get_order(conn, 1024)["status"] == "refunded"

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

