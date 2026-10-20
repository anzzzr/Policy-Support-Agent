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
    mark_order_verified,
    is_order_verified,
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
        assert "match" in result["error"].lower()

    def test_nonexistent_order(self, db):
        result = lookup_order(db, 9999, "aarav@example.com")
        assert result["ok"] is False
        assert "match" in result["error"].lower()

    def test_identical_error_for_nonexistent_order_and_wrong_email(self, db):
        res_wrong = lookup_order(db, 1001, "wrong@example.com")
        res_none = lookup_order(db, 9999, "aarav@example.com")
        assert res_wrong["ok"] is False
        assert res_none["ok"] is False
        assert res_wrong["error"] == res_none["error"] == "Order details do not match our records."

    def test_case_insensitive_email(self, db):
        result = lookup_order(db, 1001, "AARAV@EXAMPLE.COM")
        assert result["ok"] is True


# ── check_refund_eligibility ─────────────────────────────────────────

class TestCheckRefundEligibility:
    @pytest.fixture(autouse=True)
    def verify_all_orders(self, db):
        for oid in range(1001, 1030):
            mark_order_verified(db, oid)

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

    def test_exact_5000_is_eligible(self, db):
        """Order 1024: ₹5000 — exactly at the agent refund threshold."""
        result = check_refund_eligibility(db, 1024, _today=TODAY)
        assert result["eligible"] is True
        assert result["max_amount"] == 5000

    def test_day_30_is_eligible(self, db):
        """Delivery exactly 30 days ago is eligible."""
        db.execute("UPDATE orders SET delivered_at = '2026-09-21' WHERE id = 1003")
        db.commit()
        result = check_refund_eligibility(db, 1003, _today=TODAY)
        assert result["eligible"] is True

    def test_day_31_is_ineligible(self, db):
        """Delivery 31 days ago is outside the 30-day window."""
        db.execute("UPDATE orders SET delivered_at = '2026-09-20' WHERE id = 1003")
        db.commit()
        result = check_refund_eligibility(db, 1003, _today=TODAY)
        assert result["eligible"] is False
        assert "limit is 30" in result["reason"]


# ── issue_refund (guard ON) ──────────────────────────────────────────

class TestIssueRefundGuard:
    @pytest.fixture(autouse=True)
    def verify_all_orders(self, db):
        for oid in range(1001, 1030):
            mark_order_verified(db, oid)

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

    def test_day_30_is_eligible(self, db):
        """Rule 2: delivery exactly 30 days ago is eligible."""
        db.execute("UPDATE orders SET delivered_at = '2026-09-21' WHERE id = 1003")
        db.commit()
        result = issue_refund(db, 1003, 1500, _today=TODAY)
        assert result["ok"] is True
        assert result["amount"] == 1500

    def test_day_31_is_blocked(self, db):
        """Rule 2: delivery 31 days ago is blocked."""
        db.execute("UPDATE orders SET delivered_at = '2026-09-20' WHERE id = 1003")
        db.commit()
        result = issue_refund(db, 1003, 1500, _today=TODAY)
        assert result["ok"] is False
        assert "rule 2" in result["error"].lower()

    def test_partial_refund_blocked_by_guard(self, db):
        """Rule 4: partial refund is blocked by guard."""
        result = issue_refund(db, 1003, 1000, _today=TODAY)
        assert result["ok"] is False
        assert "rule 4" in result["error"].lower()
        assert "partial refund" in result["error"].lower()

    def test_exact_5000_refund_succeeds(self, db):
        """Rule 4: refund of exactly ₹5,000 succeeds."""
        result = issue_refund(db, 1024, 5000, _today=TODAY)
        assert result["ok"] is True
        assert result["amount"] == 5000

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

    def test_guard_off_allows_partial_refund(self, db, monkeypatch):
        """With GUARD=off, a partial refund is allowed."""
        monkeypatch.setenv("GUARD", "off")
        result = issue_refund(db, 1003, 1000, _today=TODAY)
        assert result["ok"] is True
        assert result["amount"] == 1000

    def test_guard_off_nonexistent_order_leaves_db_unchanged(self, db, monkeypatch):
        """With GUARD=off, a nonexistent order returns generic error and leaves DB unchanged."""
        monkeypatch.setenv("GUARD", "off")
        refund_count_before = db.execute("SELECT count(*) as count FROM refunds").fetchone()["count"]
        res = issue_refund(db, 9999, 100.0, _today=TODAY)
        assert res["ok"] is False
        assert res["success"] is False
        assert res["error"] == "Order not found."
        refund_count_after = db.execute("SELECT count(*) as count FROM refunds").fetchone()["count"]
        assert refund_count_before == refund_count_after



# ── cancel_order (guard ON) ──────────────────────────────────────────

class TestCancelOrderGuard:
    @pytest.fixture(autouse=True)
    def verify_all_orders(self, db):
        for oid in range(1001, 1030):
            mark_order_verified(db, oid)

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
    @pytest.fixture(autouse=True)
    def verify_all_orders(self, db):
        for oid in range(1001, 1030):
            mark_order_verified(db, oid)

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

    def test_explicit_null_order_id(self, db):
        """Rule 9: escalations accept explicit null order_id."""
        result = escalate_to_human(db, "Fraud reported on account", order_id=None)
        assert result["ok"] is True
        assert result["reason"] == "Fraud reported on account"
        row = db.execute("SELECT * FROM escalations WHERE id = ?", (result["escalation_id"],)).fetchone()
        assert row["order_id"] is None

    def test_dispatch_explicit_null_order_id(self, db):
        """dispatch routes escalate_to_human with order_id=None."""
        result = dispatch(db, "escalate_to_human", {"reason": "Fraud reported on account", "order_id": None})
        assert result["ok"] is True
        assert result["reason"] == "Fraud reported on account"


# ── dispatch ──────────────────────────────────────────────────────────

class TestDispatch:
    def test_routes_to_correct_tool(self, db):
        result = dispatch(db, "lookup_order", {"order_id": 1001, "email": "aarav@example.com"})
        assert result["ok"] is True
        assert result["order_id"] == 1001

    def test_unknown_tool(self, db):
        result = dispatch(db, "nonexistent_tool", {})
        assert result["ok"] is False
        assert result["success"] is False
        assert "unknown" in result["error"].lower()

    def test_missing_argument(self, db):
        result = dispatch(db, "update_address", {"order_id": 1001})
        assert result["ok"] is False
        assert result["success"] is False
        assert "missing required argument" in result["error"].lower()

    def test_extra_argument(self, db):
        result = dispatch(db, "cancel_order", {"order_id": 1001, "unexpected_key": "val"})
        assert result["ok"] is False
        assert result["success"] is False
        assert "unexpected argument" in result["error"].lower()

    def test_non_dict_arguments(self, db):
        result = dispatch(db, "cancel_order", ["not", "a", "dict"])
        assert result["ok"] is False
        assert result["success"] is False
        assert "must be a dictionary" in result["error"].lower()


# ── Step 1 Validation & Boundary Tests ─────────────────────────────────

class TestIssueRefundValidation:
    def test_reject_non_numeric_amount(self, db):
        for bad in ["five hundred", None, [], True, False]:
            res = issue_refund(db, 1003, bad, _today=TODAY)
            assert res["ok"] is False
            assert res["success"] is False
            assert "finite number" in res["error"]

    def test_reject_nan_amount(self, db):
        res = issue_refund(db, 1003, float("nan"), _today=TODAY)
        assert res["ok"] is False
        assert res["success"] is False
        assert "finite number" in res["error"]

    def test_reject_inf_amount(self, db):
        for bad in [float("inf"), float("-inf")]:
            res = issue_refund(db, 1003, bad, _today=TODAY)
            assert res["ok"] is False
            assert res["success"] is False
            assert "finite number" in res["error"]

    def test_reject_negative_and_zero_amount(self, db):
        for bad in [0, 0.0, -1, -500.50]:
            res = issue_refund(db, 1003, bad, _today=TODAY)
            assert res["ok"] is False
            assert res["success"] is False
            assert "greater than zero" in res["error"]

    def test_reject_amount_exceeding_order_total(self, db):
        mark_order_verified(db, 1003)
        # Order 1003 total is 1500
        res = issue_refund(db, 1003, 1500.01, _today=TODAY)
        assert res["ok"] is False
        assert res["success"] is False
        assert "exceeds order total" in res["error"]

    def test_cap_decimals_to_2_places(self, db):
        mark_order_verified(db, 1003)
        db.execute("UPDATE orders SET total = 123.46 WHERE id = 1003")
        db.commit()
        res = issue_refund(db, 1003, 123.4567, _today=TODAY)
        assert res["ok"] is True
        assert res["success"] is True
        assert res["amount"] == 123.46


class TestToolsTypeAndInputValidation:
    def test_lookup_order_invalid_types(self, db):
        for bad_id in ["1001", -1, 0, True, None]:
            res = lookup_order(db, bad_id, "aarav@example.com")
            assert res["ok"] is False
            assert res["success"] is False
            assert "invalid order_id" in res["error"].lower()

        for bad_email in [123, None, "", "invalid"]:
            res = lookup_order(db, 1001, bad_email)
            assert res["ok"] is False
            assert res["success"] is False
            assert "invalid email" in res["error"].lower()

    def test_lookup_order_email_spaces_and_case(self, db):
        res = lookup_order(db, 1001, "   AARAV@EXAMPLE.COM   ")
        assert res["ok"] is True
        assert res["success"] is True
        assert res["order_id"] == 1001

    def test_cancel_order_invalid_id(self, db):
        res = cancel_order(db, "not_an_int")
        assert res["ok"] is False
        assert res["success"] is False
        assert "invalid order_id" in res["error"].lower()

    def test_update_address_invalid_types_and_length(self, db):
        res = update_address(db, "1001", "New address")
        assert res["ok"] is False
        assert res["success"] is False

        res = update_address(db, 1001, 12345)
        assert res["ok"] is False
        assert res["success"] is False

        # Over sane length
        long_addr = "A" * 350
        res = update_address(db, 1001, long_addr)
        assert res["ok"] is False
        assert res["success"] is False
        assert "maximum length" in res["error"].lower()

    def test_update_address_strips_control_characters(self, db):
        mark_order_verified(db, 1001)
        # Address with null byte and control character
        res = update_address(db, 1001, "123 MG\x00 Road\x07, Goa")
        assert res["ok"] is True
        assert res["success"] is True
        assert res["address"] == "123 MG Road, Goa"

    def test_escalate_invalid_inputs(self, db):
        res = escalate_to_human(db, "", order_id=1001)
        assert res["ok"] is False
        assert res["success"] is False

        res = escalate_to_human(db, "Reason", order_id="invalid")
        assert res["ok"] is False
        assert res["success"] is False


# ── Step 2 Identity Enforcement Tests ──────────────────────────────────

class TestIdentityEnforcement:
    def test_direct_cancel_fails_without_verification(self, db):
        """Rule 1: Direct cancel_order without lookup must fail when GUARD=on."""
        res = cancel_order(db, 1001)
        assert res["ok"] is False
        assert res["success"] is False
        assert "rule 1" in res["error"].lower()
        assert "policy violation" in res["error"].lower()

    def test_direct_issue_refund_fails_without_verification(self, db):
        """Rule 1: Direct issue_refund without lookup must fail when GUARD=on."""
        res = issue_refund(db, 1003, 1500, _today=TODAY)
        assert res["ok"] is False
        assert res["success"] is False
        assert "rule 1" in res["error"].lower()

    def test_direct_update_address_fails_without_verification(self, db):
        """Rule 1: Direct update_address without lookup must fail when GUARD=on."""
        res = update_address(db, 1001, "New Address")
        assert res["ok"] is False
        assert res["success"] is False
        assert "rule 1" in res["error"].lower()

    def test_direct_check_eligibility_fails_without_verification(self, db):
        """Rule 1: check_refund_eligibility without lookup must fail when GUARD=on."""
        res = check_refund_eligibility(db, 1003, _today=TODAY)
        assert res["ok"] is False
        assert res["success"] is False
        assert res["eligible"] is False
        assert "rule 1" in res["error"].lower()

    def test_failed_lookup_does_not_verify_order(self, db):
        """Lookup with incorrect email leaves the order unverified."""
        lookup_res = lookup_order(db, 1001, "wrong@example.com")
        assert lookup_res["ok"] is False

        cancel_res = cancel_order(db, 1001)
        assert cancel_res["ok"] is False
        assert "rule 1" in cancel_res["error"].lower()

    def test_successful_lookup_verifies_order_for_subsequent_actions(self, db):
        """Lookup with correct email authorizes subsequent actions on that order."""
        lookup_res = lookup_order(db, 1001, "aarav@example.com")
        assert lookup_res["ok"] is True
        assert is_order_verified(db, 1001) is True

        # Now cancellation succeeds
        cancel_res = cancel_order(db, 1001)
        assert cancel_res["ok"] is True
        assert cancel_res["status"] == "cancelled"

    def test_guard_off_disables_identity_enforcement(self, db, monkeypatch):
        """With GUARD=off, identity verification check is bypassed."""
        monkeypatch.setenv("GUARD", "off")
        res = cancel_order(db, 1001)
        assert res["ok"] is True
        assert res["status"] == "cancelled"

    def test_identical_text_for_nonexistent_and_unverified_orders(self, db):
        """issue_refund, cancel_order, update_address, check_refund_eligibility
        must return identical error format for nonexistent order vs existing unverified order.
        """
        template = "Policy violation (rule 1): Order {oid} has not been verified in this session. Call lookup_order with customer email first."

        # 1. issue_refund
        res_none = issue_refund(db, 9999, 100.0, _today=TODAY)
        res_unver = issue_refund(db, 1003, 1500.0, _today=TODAY)
        assert res_none["error"] == template.format(oid=9999)
        assert res_unver["error"] == template.format(oid=1003)

        # 2. cancel_order
        res_none = cancel_order(db, 9999)
        res_unver = cancel_order(db, 1001)
        assert res_none["error"] == template.format(oid=9999)
        assert res_unver["error"] == template.format(oid=1001)

        # 3. update_address
        res_none = update_address(db, 9999, "999 St")
        res_unver = update_address(db, 1001, "999 St")
        assert res_none["error"] == template.format(oid=9999)
        assert res_unver["error"] == template.format(oid=1001)

        # 4. check_refund_eligibility
        res_none = check_refund_eligibility(db, 9999, _today=TODAY)
        res_unver = check_refund_eligibility(db, 1003, _today=TODAY)
        assert res_none["error"] == template.format(oid=9999)
        assert res_unver["error"] == template.format(oid=1003)

    def test_huge_amount_refund_identical_text_for_nonexistent_and_unverified(self, db):
        """A huge refund amount must not disclose order existence.
        Both nonexistent order and unverified order must return identical rule 1 error.
        """
        template = "Policy violation (rule 1): Order {oid} has not been verified in this session. Call lookup_order with customer email first."
        huge_amount = 999_999_999.0
        res_none = issue_refund(db, 9999, huge_amount, _today=TODAY)
        res_unver = issue_refund(db, 1003, huge_amount, _today=TODAY)
        assert res_none["ok"] is False and res_unver["ok"] is False
        assert res_none["error"] == template.format(oid=9999)
        assert res_unver["error"] == template.format(oid=1003)



