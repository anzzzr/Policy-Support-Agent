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
