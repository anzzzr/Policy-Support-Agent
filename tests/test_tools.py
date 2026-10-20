"""
Tests for app.tools — every tool function and every guard rule.

Each test class covers one tool.  For write-tools, we test both the
allowed path and each guard rule that should block the call.
"""

import pytest
from datetime import date

from app.db import get_db, seed, get_order, get_refunds_for_order
from app.tools import (
    lookup_order,
    check_refund_eligibility,
    issue_refund,
    cancel_order,
    update_address,
    escalate_to_human,
    dispatch,
)

# All seed data was designed around this date.
TODAY = date(2026, 10, 21)


# ── Fixtures ──────────────────────────────────────────────────────────

@pytest.fixture
def db():
    """Seeded in-memory database — fresh for every test."""
    conn = get_db(":memory:")
    seed(conn)
    yield conn
    conn.close()


# ── lookup_order ──────────────────────────────────────────────────────

class TestLookupOrder:
    def test_success(self, db):
        result = lookup_order(db, 1001, "aarav@example.com")
        assert result["ok"] is True
        assert result["order_id"] == 1001
        assert result["status"] == "processing"
        assert result["customer_name"] == "Aarav Sharma"

    def test_wrong_email(self, db):
        result = lookup_order(db, 1001, "wrong@example.com")
        assert result["ok"] is False
        assert "not found" in result["error"].lower() or "mismatch" in result["error"].lower()

    def test_nonexistent_order(self, db):
        result = lookup_order(db, 9999, "aarav@example.com")
        assert result["ok"] is False

    def test_case_insensitive_email(self, db):
        result = lookup_order(db, 1001, "AARAV@EXAMPLE.COM")
        assert result["ok"] is True


# ── check_refund_eligibility ─────────────────────────────────────────

class TestCheckRefundEligibility:
    def test_eligible(self, db):
        """Order 1003: delivered 2026-10-05, ₹1500, not final sale, no refund."""
        result = check_refund_eligibility(db, 1003, _today=TODAY)
        assert result["eligible"] is True
        assert result["max_amount"] == 1500

    def test_not_delivered(self, db):
        """Order 1001 is still processing."""
        result = check_refund_eligibility(db, 1001, _today=TODAY)
        assert result["eligible"] is False
        assert "processing" in result["reason"]

    def test_final_sale(self, db):
        """Order 1005: delivered, but final sale."""
        result = check_refund_eligibility(db, 1005, _today=TODAY)
        assert result["eligible"] is False
        assert "final" in result["reason"].lower()

    def test_too_old(self, db):
        """Order 1004: delivered 2026-08-15 — 67 days ago."""
        result = check_refund_eligibility(db, 1004, _today=TODAY)
        assert result["eligible"] is False
        assert "days" in result["reason"]

    def test_already_refunded(self, db):
        """Order 1022: already has a refund row."""
        result = check_refund_eligibility(db, 1022, _today=TODAY)
        assert result["eligible"] is False
        assert "already" in result["reason"].lower()

    def test_above_5000_must_escalate(self, db):
        """Order 1006: ₹7500 — eligible except amount exceeds ₹5000."""
        result = check_refund_eligibility(db, 1006, _today=TODAY)
        assert result["eligible"] is False
        assert "5,000" in result["reason"] or "5000" in result["reason"]

    def test_nonexistent_order(self, db):
        result = check_refund_eligibility(db, 9999, _today=TODAY)
        assert result["eligible"] is False

    def test_just_under_5000_is_eligible(self, db):
        """Order 1012: ₹4999 — just under the escalation threshold."""
        result = check_refund_eligibility(db, 1012, _today=TODAY)
        assert result["eligible"] is True
        assert result["max_amount"] == 4999


# ── issue_refund (guard ON) ──────────────────────────────────────────

class TestIssueRefundGuard:
    def test_success(self, db):
        """Order 1003: all rules pass."""
        result = issue_refund(db, 1003, 1500, _today=TODAY)
        assert result["ok"] is True
        assert result["amount"] == 1500
        # DB state should reflect the refund
        order = get_order(db, 1003)
        assert order["status"] == "refunded"

    def test_blocks_not_delivered(self, db):
        """Rule 2: order 1001 is processing, not delivered."""
        result = issue_refund(db, 1001, 2500, _today=TODAY)
        assert result["ok"] is False
        assert "rule 2" in result["error"].lower()
        # DB must be unchanged
        assert get_order(db, 1001)["status"] == "processing"

    def test_blocks_too_old(self, db):
        """Rule 2: order 1004 delivered >30 days ago."""
        result = issue_refund(db, 1004, 2000, _today=TODAY)
        assert result["ok"] is False
        assert "rule 2" in result["error"].lower()

    def test_blocks_final_sale(self, db):
        """Rule 3: order 1005 is final sale."""
        result = issue_refund(db, 1005, 800, _today=TODAY)
        assert result["ok"] is False
        assert "rule 3" in result["error"].lower()

    def test_blocks_above_5000(self, db):
        """Rule 4: amount exceeds ₹5000."""
        result = issue_refund(db, 1006, 7500, _today=TODAY)
        assert result["ok"] is False
        assert "rule 4" in result["error"].lower()

    def test_blocks_already_refunded(self, db):
        """Rule 5: order 1022 already has a refund."""
        result = issue_refund(db, 1022, 1200, _today=TODAY)
        assert result["ok"] is False
        assert "rule 5" in result["error"].lower()

    def test_blocks_nonexistent_order(self, db):
        result = issue_refund(db, 9999, 100, _today=TODAY)
        assert result["ok"] is False


class TestIssueRefundGuardOff:
    def test_guard_off_allows_blocked_action(self, db, monkeypatch):
        """With GUARD=off, a normally-blocked refund goes through."""
        monkeypatch.setenv("GUARD", "off")
        # Order 1005 is final sale — guard would block it
        result = issue_refund(db, 1005, 800, _today=TODAY)
        assert result["ok"] is True
        assert get_order(db, 1005)["status"] == "refunded"


# ── cancel_order (guard ON) ──────────────────────────────────────────

class TestCancelOrderGuard:
    def test_success(self, db):
        """Order 1001 is processing — cancellation allowed."""
        result = cancel_order(db, 1001)
        assert result["ok"] is True
        assert result["status"] == "cancelled"
        assert get_order(db, 1001)["status"] == "cancelled"

    def test_blocks_shipped(self, db):
        """Rule 6: order 1002 is shipped."""
        result = cancel_order(db, 1002)
        assert result["ok"] is False
        assert "rule 6" in result["error"].lower()
        assert get_order(db, 1002)["status"] == "shipped"

    def test_blocks_delivered(self, db):
        """Rule 6: order 1003 is delivered."""
        result = cancel_order(db, 1003)
        assert result["ok"] is False
        assert "rule 6" in result["error"].lower()

    def test_blocks_already_cancelled(self, db):
        """Rule 6: order 1007 is already cancelled."""
        result = cancel_order(db, 1007)
        assert result["ok"] is False
        assert "rule 6" in result["error"].lower()

    def test_blocks_nonexistent(self, db):
        result = cancel_order(db, 9999)
        assert result["ok"] is False


class TestCancelOrderGuardOff:
    def test_guard_off_allows_shipped_cancel(self, db, monkeypatch):
        """With GUARD=off, even a shipped order can be cancelled."""
        monkeypatch.setenv("GUARD", "off")
        result = cancel_order(db, 1002)
        assert result["ok"] is True
        assert get_order(db, 1002)["status"] == "cancelled"


# ── update_address (guard ON) ────────────────────────────────────────

class TestUpdateAddressGuard:
    def test_success(self, db):
        """Order 1001 is processing — address change allowed."""
        result = update_address(db, 1001, "999 New Street, Goa")
        assert result["ok"] is True
        assert result["address"] == "999 New Street, Goa"
        assert get_order(db, 1001)["address"] == "999 New Street, Goa"

    def test_blocks_shipped(self, db):
        """Rule 7: order 1002 is shipped."""
        result = update_address(db, 1002, "999 New Street, Goa")
        assert result["ok"] is False
        assert "rule 7" in result["error"].lower()
        # Address must not change
        assert get_order(db, 1002)["address"] == "123 MG Road, Mumbai"

    def test_blocks_delivered(self, db):
        """Rule 7: order 1003 is delivered."""
        result = update_address(db, 1003, "999 New Street, Goa")
        assert result["ok"] is False
        assert "rule 7" in result["error"].lower()

    def test_blocks_nonexistent(self, db):
        result = update_address(db, 9999, "Anywhere")
        assert result["ok"] is False


class TestUpdateAddressGuardOff:
    def test_guard_off_allows_shipped_update(self, db, monkeypatch):
        """With GUARD=off, even a shipped order's address can change."""
        monkeypatch.setenv("GUARD", "off")
        result = update_address(db, 1002, "999 New Street, Goa")
        assert result["ok"] is True
        assert get_order(db, 1002)["address"] == "999 New Street, Goa"


# ── escalate_to_human ────────────────────────────────────────────────

class TestEscalateToHuman:
    def test_with_order(self, db):
        result = escalate_to_human(db, "Refund above ₹5000", order_id=1006)
        assert result["ok"] is True
        assert result["reason"] == "Refund above ₹5000"

    def test_without_order(self, db):
        """Escalation for abusive language may not have an order."""
        result = escalate_to_human(db, "Abusive language")
        assert result["ok"] is True
        assert result["reason"] == "Abusive language"


# ── dispatch ──────────────────────────────────────────────────────────

class TestDispatch:
    def test_routes_to_correct_tool(self, db):
        result = dispatch(db, "lookup_order", {"order_id": 1001, "email": "aarav@example.com"})
        assert result["ok"] is True
        assert result["order_id"] == 1001

    def test_unknown_tool(self, db):
        result = dispatch(db, "nonexistent_tool", {})
        assert result["ok"] is False
        assert "unknown" in result["error"].lower()
