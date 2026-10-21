"""
Database layer: schema, seed data, and query helpers.

Everything is plain sqlite3 — no ORM.  Helpers take a connection and return
dicts (via sqlite3.Row) so the rest of the app never writes raw SQL.
"""

import sqlite3
import os
from datetime import datetime, date, timedelta


# ── Connection ────────────────────────────────────────────────────────

def get_db(path: str = "store.db") -> sqlite3.Connection:
    """Open (or create) a database and ensure the schema exists."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row          # rows behave like dicts
    conn.execute("PRAGMA journal_mode=WAL") # safer for concurrent reads
    conn.execute("PRAGMA foreign_keys=ON")  # enforce FK constraints
    create_tables(conn)
    return conn


def get_connection(path: str = ":memory:", seed_data: bool = True) -> sqlite3.Connection:
    """Convenience helper returning a connection with schema and seeded data."""
    conn = get_db(path)
    if seed_data:
        seed(conn)
    return conn


# ── Schema ────────────────────────────────────────────────────────────

def create_tables(conn: sqlite3.Connection) -> None:
    """Create all tables idempotently (IF NOT EXISTS)."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS customers (
            id      INTEGER PRIMARY KEY,
            name    TEXT    NOT NULL,
            email   TEXT    NOT NULL UNIQUE
        );

        CREATE TABLE IF NOT EXISTS orders (
            id            INTEGER PRIMARY KEY,
            customer_id   INTEGER NOT NULL REFERENCES customers(id),
            status        TEXT    NOT NULL DEFAULT 'processing'
                          CHECK (status IN (
                              'processing', 'shipped', 'delivered',
                              'cancelled', 'refunded'
                          )),
            total         REAL    NOT NULL,
            delivered_at  TEXT,               -- ISO-8601 date; NULL until delivered
            is_final_sale INTEGER NOT NULL DEFAULT 0,
            address       TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS refunds (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id    INTEGER NOT NULL REFERENCES orders(id),
            amount      REAL    NOT NULL,
            created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS escalations (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id    INTEGER REFERENCES orders(id),  -- nullable: not every escalation is about a specific order
            reason_code TEXT    NOT NULL,
            note        TEXT    NOT NULL DEFAULT '',
            created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
        );
    """)


# ── Seed data ─────────────────────────────────────────────────────────
# ~12 customers, ~25 orders covering every branch in the policy.
# Dates are pinned so tests are deterministic.
# "Recent" = within 30 days of 2026-10-21.  "Old" = outside that window.

def seed(conn: sqlite3.Connection, ref_date: date | None = None) -> None:
    """Insert demo data. Safe to call on an empty DB only.

    Delivery dates are computed relative to EVAL_TODAY (or MOCK_DATE / ref_date).
    """
    if ref_date is None:
        ref_str = os.environ.get("EVAL_TODAY") or os.environ.get("MOCK_DATE") or "2026-10-21"
        ref_date = date.fromisoformat(ref_str)

    def rel(days_ago: int) -> str:
        return (ref_date - timedelta(days=days_ago)).isoformat()

    customers = [
        (1,  "Aarav Sharma",   "aarav@example.com"),
        (2,  "Priya Patel",    "priya@example.com"),
        (3,  "Rahul Gupta",    "rahul@example.com"),
        (4,  "Sneha Reddy",    "sneha@example.com"),
        (5,  "Vikram Singh",   "vikram@example.com"),
        (6,  "Ananya Joshi",   "ananya@example.com"),
        (7,  "Karthik Nair",   "karthik@example.com"),
        (8,  "Deepa Menon",    "deepa@example.com"),
        (9,  "Arjun Kumar",    "arjun@example.com"),
        (10, "Meera Iyer",     "meera@example.com"),
        (11, "Rohan Desai",    "rohan@example.com"),
        (12, "Kavita Rao",     "kavita@example.com"),
    ]
    conn.executemany(
        "INSERT INTO customers (id, name, email) VALUES (?, ?, ?)",
        customers,
    )

    # (id, customer_id, status, total, delivered_at, is_final_sale, address)
    orders = [
        # ── Aarav: processing + shipped ──
        (1001, 1, "processing", 2500, None,     0, "123 MG Road, Mumbai"),
        (1002, 1, "shipped",    3000, None,     0, "123 MG Road, Mumbai"),

        # ── Priya: refundable + too old ──
        (1003, 2, "delivered",  1500, rel(16),  0, "45 Nehru St, Delhi"),
        (1004, 2, "delivered",  2000, rel(67),  0, "45 Nehru St, Delhi"),

        # ── Rahul: final-sale + high-value escalation + processing ──
        (1005, 3, "delivered",   800, rel(11),  1, "78 Park Ave, Bangalore"),
        (1006, 3, "delivered",  7500, rel(13),  0, "78 Park Ave, Bangalore"),
        (1025, 3, "processing", 1100, None,     0, "78 Park Ave, Bangalore"),

        # ── Sneha: already cancelled + already refunded ──
        (1007, 4, "cancelled",  1200, None,     0, "12 Lake Rd, Hyderabad"),
        (1008, 4, "refunded",    900, rel(36),  0, "12 Lake Rd, Hyderabad"),

        # ── Vikram: processing + refundable ──
        (1009, 5, "processing", 4500, None,     0, "90 Ring Rd, Pune"),
        (1010, 5, "delivered",  3200, rel(20),  0, "90 Ring Rd, Pune"),

        # ── Ananya: shipped (can't cancel) + just-under-5000 refundable ──
        (1011, 6, "shipped",    6000, None,     0, "34 Hill View, Chennai"),
        (1012, 6, "delivered",  4999, rel(26),  0, "34 Hill View, Chennai"),

        # ── Karthik: processing final-sale + needs-escalation (>5000) ──
        (1013, 7, "processing", 1800, None,     1, "56 Temple St, Kochi"),
        (1014, 7, "delivered",  5001, rel(9),   0, "56 Temple St, Kochi"),
        (1028, 7, "delivered",  5000.01, rel(10), 0, "56 Temple St, Kochi"),

        # ── Deepa: refundable + processing + exact boundary 30/31 days ──
        (1015, 8, "delivered",  2500, rel(18),  0, "23 Gandhi Nagar, Jaipur"),
        (1016, 8, "processing", 3300, None,     0, "23 Gandhi Nagar, Jaipur"),
        (1026, 8, "delivered",  2000, rel(30),  0, "23 Gandhi Nagar, Jaipur"),
        (1027, 8, "delivered",  2000, rel(31),  0, "23 Gandhi Nagar, Jaipur"),

        # ── Arjun: too old + shipped ──
        (1017, 9, "delivered",  4000, rel(41),  0, "67 Station Rd, Lucknow"),
        (1018, 9, "shipped",    1500, None,     0, "67 Station Rd, Lucknow"),

        # ── Meera: final-sale delivered + processing ──
        (1019, 10, "delivered",  950, rel(14),  1, "89 Market Lane, Ahmedabad"),
        (1020, 10, "processing", 2200, None,    0, "89 Market Lane, Ahmedabad"),

        # ── Rohan: refundable + already-refunded (double refund test) ──
        # Note: Order 1022 deliberately has status 'delivered' despite having a refund row,
        # in order to isolate Rule 5 (single refund per order check) independently of status checks.
        (1021, 11, "delivered", 3500, rel(15),  0, "11 College Rd, Nagpur"),
        (1022, 11, "delivered", 1200, rel(19),  0, "11 College Rd, Nagpur"),

        # ── Kavita: processing (high value) + refundable (exactly 5000) ──
        (1023, 12, "processing", 8000, None,    0, "44 Ashok Marg, Bhopal"),
        (1024, 12, "delivered",  5000, rel(12),  0, "44 Ashok Marg, Bhopal"),
    ]
    conn.executemany(
        "INSERT INTO orders (id, customer_id, status, total, delivered_at, is_final_sale, address) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        orders,
    )

    # Pre-existing refunds — needed to test "only one refund per order"
    conn.execute(
        "INSERT INTO refunds (order_id, amount, created_at) VALUES (?, ?, ?)",
        (1008, 900, "2026-09-20T10:00:00"),   # Sneha's refunded order
    )
    conn.execute(
        "INSERT INTO refunds (order_id, amount, created_at) VALUES (?, ?, ?)",
        (1022, 1200, "2026-10-05T14:30:00"),  # Rohan's order — deliberately has status 'delivered' to isolate Rule 5
    )

    conn.commit()


# ── Query helpers ─────────────────────────────────────────────────────

def get_order(conn: sqlite3.Connection, order_id: int) -> dict | None:
    """Fetch a single order by ID, or None if it doesn't exist."""
    row = conn.execute(
        "SELECT * FROM orders WHERE id = ?", (order_id,)
    ).fetchone()
    return dict(row) if row else None


def get_customer(conn: sqlite3.Connection, customer_id: int) -> dict | None:
    """Fetch a single customer by ID, or None."""
    row = conn.execute(
        "SELECT * FROM customers WHERE id = ?", (customer_id,)
    ).fetchone()
    return dict(row) if row else None


def lookup_order(conn: sqlite3.Connection, order_id: int, email: str) -> dict | None:
    """Identity check (policy rule 1).

    Returns the order only if the given email matches the customer on the
    order.  This is the FIRST thing the agent must do before any action.
    """
    if not isinstance(email, str) or not isinstance(order_id, int) or isinstance(order_id, bool):
        return None
    email_clean = email.strip().casefold()
    row = conn.execute(
        """
        SELECT o.* FROM orders o
        JOIN customers c ON o.customer_id = c.id
        WHERE o.id = ? AND LOWER(TRIM(c.email)) = ?
        """,
        (order_id, email_clean),
    ).fetchone()
    return dict(row) if row else None


def get_refunds_for_order(conn: sqlite3.Connection, order_id: int) -> list[dict]:
    """Return all refunds for an order (usually 0 or 1)."""
    rows = conn.execute(
        "SELECT * FROM refunds WHERE order_id = ?", (order_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def create_refund(conn: sqlite3.Connection, order_id: int, amount: float) -> dict:
    """Insert a refund row AND mark the order as 'refunded'.

    We do both in one helper because a refund without the status change
    would leave the database in an inconsistent state.
    """
    cursor = conn.execute(
        "INSERT INTO refunds (order_id, amount) VALUES (?, ?)",
        (order_id, amount),
    )
    conn.execute(
        "UPDATE orders SET status = 'refunded' WHERE id = ?", (order_id,)
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM refunds WHERE id = ?", (cursor.lastrowid,)
    ).fetchone()
    return dict(row)


def cancel_order(conn: sqlite3.Connection, order_id: int) -> dict:
    """Set order status to 'cancelled'.  Returns the updated order."""
    conn.execute(
        "UPDATE orders SET status = 'cancelled' WHERE id = ?", (order_id,)
    )
    conn.commit()
    return get_order(conn, order_id)


def update_order_address(conn: sqlite3.Connection, order_id: int, new_address: str) -> dict:
    """Change the shipping address.  Returns the updated order."""
    conn.execute(
        "UPDATE orders SET address = ? WHERE id = ?", (new_address, order_id)
    )
    conn.commit()
    return get_order(conn, order_id)


def update_order_status(conn: sqlite3.Connection, order_id: int, status: str) -> dict | None:
    """Update order status directly.  Returns the updated order."""
    conn.execute(
        "UPDATE orders SET status = ? WHERE id = ?", (status, order_id)
    )
    conn.commit()
    return get_order(conn, order_id)


def create_escalation(
    conn: sqlite3.Connection,
    order_id: int | None,
    reason_code: str,
    note: str = "",
) -> dict:
    """Log an escalation for human review. order_id can be None."""
    cursor = conn.execute(
        "INSERT INTO escalations (order_id, reason_code, note) VALUES (?, ?, ?)",
        (order_id, reason_code, note),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM escalations WHERE id = ?", (cursor.lastrowid,)
    ).fetchone()
    return dict(row)
