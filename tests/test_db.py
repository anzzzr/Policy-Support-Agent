"""
Unit tests for app.db — schema, seed data, and every query helper.

Each test gets a fresh in-memory database so tests never interfere with
each other.
"""

import pytest
from app.db import (
    get_db,
    get_connection,
    seed,
    get_order,
    get_customer,
    lookup_order,
    get_refunds_for_order,
    create_refund,
    cancel_order,
    update_order_address,
    update_order_status,
    create_escalation,
)


# ── Fixtures ──────────────────────────────────────────────────────────

@pytest.fixture
def db():
    """Unseeded in-memory database — just the schema."""
    conn = get_db(":memory:")
    yield conn
    conn.close()


@pytest.fixture
def seeded_db():
    """In-memory database with full seed data."""
    conn = get_db(":memory:")
    seed(conn)
    yield conn
    conn.close()


# ── Schema tests ──────────────────────────────────────────────────────

class TestSchema:
    def test_tables_exist(self, db):
        """All four tables should be created by get_db."""
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"customers", "orders", "refunds", "escalations"} <= tables

    def test_foreign_keys_enabled(self, db):
        """FK enforcement must be on — we rely on it for data integrity."""
        fk = db.execute("PRAGMA foreign_keys").fetchone()[0]
        assert fk == 1

    def test_order_status_check_constraint(self, db):
        """Inserting an invalid status should fail."""
        db.execute(
            "INSERT INTO customers (id, name, email) VALUES (99, 'Test', 'test@x.com')"
        )
        with pytest.raises(Exception):  # IntegrityError
            db.execute(
                "INSERT INTO orders (id, customer_id, status, total, address) "
                "VALUES (9999, 99, 'bogus', 100, '123 St')"
            )


# ── Seed tests ────────────────────────────────────────────────────────

class TestSeed:
    def test_customer_count(self, seeded_db):
        count = seeded_db.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
        assert count == 12

    def test_order_count(self, seeded_db):
        count = seeded_db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        assert count == 25

    def test_pre_existing_refunds(self, seeded_db):
        """Two orders start with refunds (for 'no double refund' tests)."""
        count = seeded_db.execute("SELECT COUNT(*) FROM refunds").fetchone()[0]
        assert count == 2

    def test_all_statuses_present(self, seeded_db):
        """Seed data should cover every valid order status."""
        statuses = {
            row[0]
            for row in seeded_db.execute(
                "SELECT DISTINCT status FROM orders"
            ).fetchall()
        }
        assert statuses == {"processing", "shipped", "delivered", "cancelled", "refunded"}

    def test_has_final_sale_orders(self, seeded_db):
        count = seeded_db.execute(
            "SELECT COUNT(*) FROM orders WHERE is_final_sale = 1"
        ).fetchone()[0]
        assert count >= 2, "Need at least 2 final-sale orders for policy testing"


# ── get_order ─────────────────────────────────────────────────────────

class TestGetOrder:
    def test_existing_order(self, seeded_db):
        order = get_order(seeded_db, 1001)
        assert order is not None
        assert order["id"] == 1001
        assert order["status"] == "processing"
        assert order["total"] == 2500

    def test_nonexistent_order(self, seeded_db):
        assert get_order(seeded_db, 9999) is None


# ── get_customer ──────────────────────────────────────────────────────

class TestGetCustomer:
    def test_existing_customer(self, seeded_db):
        c = get_customer(seeded_db, 1)
        assert c is not None
        assert c["name"] == "Aarav Sharma"
        assert c["email"] == "aarav@example.com"

    def test_nonexistent_customer(self, seeded_db):
        assert get_customer(seeded_db, 9999) is None


# ── lookup_order (identity verification) ──────────────────────────────

class TestLookupOrder:
    def test_correct_email(self, seeded_db):
        """Order 1001 belongs to customer 1 (aarav@example.com)."""
        order = lookup_order(seeded_db, 1001, "aarav@example.com")
        assert order is not None
        assert order["id"] == 1001

    def test_wrong_email(self, seeded_db):
        """Wrong email → None (identity not verified)."""
        assert lookup_order(seeded_db, 1001, "wrong@example.com") is None

    def test_case_insensitive_email(self, seeded_db):
        """Email comparison should be case-insensitive."""
        order = lookup_order(seeded_db, 1001, "AARAV@EXAMPLE.COM")
        assert order is not None

    def test_nonexistent_order(self, seeded_db):
        assert lookup_order(seeded_db, 9999, "aarav@example.com") is None


# ── get_refunds_for_order ─────────────────────────────────────────────

class TestGetRefundsForOrder:
    def test_order_with_refund(self, seeded_db):
        """Order 1008 has a pre-existing refund."""
        refunds = get_refunds_for_order(seeded_db, 1008)
        assert len(refunds) == 1
        assert refunds[0]["amount"] == 900

    def test_order_without_refund(self, seeded_db):
        refunds = get_refunds_for_order(seeded_db, 1001)
        assert refunds == []


# ── create_refund ─────────────────────────────────────────────────────

class TestCreateRefund:
    def test_creates_refund_and_updates_status(self, seeded_db):
        """Refunding order 1003 should insert a refund AND set status to 'refunded'."""
        refund = create_refund(seeded_db, 1003, 1500)

        assert refund["order_id"] == 1003
        assert refund["amount"] == 1500

        # Status should now be 'refunded'
        order = get_order(seeded_db, 1003)
        assert order["status"] == "refunded"

    def test_refund_appears_in_list(self, seeded_db):
        create_refund(seeded_db, 1003, 1500)
        refunds = get_refunds_for_order(seeded_db, 1003)
        assert len(refunds) == 1


# ── cancel_order ──────────────────────────────────────────────────────

class TestCancelOrder:
    def test_cancels_processing_order(self, seeded_db):
        order = cancel_order(seeded_db, 1001)
        assert order["status"] == "cancelled"

    def test_cancel_is_persisted(self, seeded_db):
        cancel_order(seeded_db, 1001)
        order = get_order(seeded_db, 1001)
        assert order["status"] == "cancelled"


# ── update_order_address ──────────────────────────────────────────────

class TestUpdateOrderAddress:
    def test_changes_address(self, seeded_db):
        new_addr = "999 New Street, Goa"
        order = update_order_address(seeded_db, 1001, new_addr)
        assert order["address"] == new_addr

    def test_address_change_is_persisted(self, seeded_db):
        new_addr = "999 New Street, Goa"
        update_order_address(seeded_db, 1001, new_addr)
        order = get_order(seeded_db, 1001)
        assert order["address"] == new_addr


# ── create_escalation ────────────────────────────────────────────────

class TestCreateEscalation:
    def test_creates_escalation_with_order(self, seeded_db):
        esc = create_escalation(seeded_db, 1006, "Refund above Rs 5000")
        assert esc["order_id"] == 1006
        assert esc["reason"] == "Refund above Rs 5000"
        assert esc["id"] is not None

    def test_creates_escalation_without_order(self, seeded_db):
        """Escalations for abusive language etc. may not have an order."""
        esc = create_escalation(seeded_db, None, "Abusive language")
        assert esc["order_id"] is None
        assert esc["reason"] == "Abusive language"

    def test_escalation_is_persisted(self, seeded_db):
        create_escalation(seeded_db, 1006, "Test")
        rows = seeded_db.execute("SELECT * FROM escalations").fetchall()
        assert len(rows) == 1


# ── get_connection & update_order_status ──────────────────────────────

class TestGetConnection:
    def test_get_connection_default_seeded(self):
        conn = get_connection()
        try:
            order = get_order(conn, 1001)
            assert order is not None
            assert order["status"] == "processing"
            assert order["total"] == 2500
        finally:
            conn.close()

    def test_get_connection_unseeded(self):
        conn = get_connection(seed_data=False)
        try:
            order = get_order(conn, 1001)
            assert order is None
        finally:
            conn.close()


class TestUpdateOrderStatus:
    def test_update_order_status(self, seeded_db):
        updated = update_order_status(seeded_db, 1002, "cancelled")
        assert updated["status"] == "cancelled"
        fetched = get_order(seeded_db, 1002)
        assert fetched["status"] == "cancelled"

