"""
Tool definitions, JSON schemas for the LLM, and policy guards.

Each tool is a plain function: (conn, **llm_args) -> dict.
Write-tools have a guard that re-checks policy rules in Python so even if
the LLM hallucinates a bad call, the database stays correct.

Set GUARD=off (env var) to disable guards for evaluation ablation only.
"""

import logging
import os
import sqlite3
from datetime import date

from app import db

log = logging.getLogger(__name__)


# ── Guard helpers ─────────────────────────────────────────────────────

def _get_today(_override: date | None = None) -> date:
    """Return effective date for 30-day policy checks."""
    if _override:
        return _override
    env_date = os.environ.get("MOCK_DATE")
    if env_date:
        return date.fromisoformat(env_date)
    return date.today()


def _guard_enabled() -> bool:
    """Check whether policy guards are active (default: yes)."""
    return os.environ.get("GUARD", "on").lower() != "off"


def _block(rule: str, detail: str) -> dict:
    """Return an error dict and log when the guard blocks a call.

    Using a dedicated helper keeps the logging consistent and makes it
    easy to search logs for "blocked_violation".
    """
    log.warning("blocked_violation rule=%s %s", rule, detail)
    return {"ok": False, "error": f"Policy violation (rule {rule}): {detail}"}


# ── Read tools (no guard needed) ─────────────────────────────────────

def lookup_order(
    conn: sqlite3.Connection, order_id: int, email: str
) -> dict:
    """Identity verification (policy rule 1).

    Returns order details only if the email matches the customer on file.
    """
    order = db.lookup_order(conn, order_id, email)
    if order is None:
        return {"ok": False, "error": "Order not found or email does not match."}
    customer = db.get_customer(conn, order["customer_id"])
    return {
        "ok": True,
        "order_id": order["id"],
        "status": order["status"],
        "total": order["total"],
        "address": order["address"],
        "delivered_at": order["delivered_at"],
        "is_final_sale": bool(order["is_final_sale"]),
        "customer_name": customer["name"],
    }


def check_refund_eligibility(
    conn: sqlite3.Connection, order_id: int, *, _today: date | None = None
) -> dict:
    """Check all refund rules (2-5) without changing anything.

    _today is injectable for deterministic tests; defaults to today.
    """
    order = db.get_order(conn, order_id)
    if order is None:
        return {"eligible": False, "reason": "Order not found."}

    # Rule 2a: must be delivered
    if order["status"] != "delivered":
        return {
            "eligible": False,
            "reason": f"Order status is '{order['status']}', not 'delivered'.",
        }

    # Rule 3: no final-sale refunds
    if order["is_final_sale"]:
        return {"eligible": False, "reason": "Final-sale items cannot be refunded."}

    # Rule 2b: within 30 days of delivery
    today = _get_today(_today)
    delivered = date.fromisoformat(order["delivered_at"])
    days_since = (today - delivered).days
    if days_since > 30:
        return {
            "eligible": False,
            "reason": f"Delivery was {days_since} days ago (limit is 30).",
        }

    # Rule 5: one refund per order
    if db.get_refunds_for_order(conn, order_id):
        return {
            "eligible": False,
            "reason": "A refund has already been issued for this order.",
        }

    # Rule 4: >5000 must be escalated
    if order["total"] > 5000:
        return {
            "eligible": False,
            "reason": (
                f"Refund amount ₹{order['total']:.0f} exceeds ₹5,000. "
                "Must be escalated to a human."
            ),
        }

    return {
        "eligible": True,
        "reason": "Eligible for refund.",
        "max_amount": order["total"],
    }


# ── Write tools (guarded) ────────────────────────────────────────────

def issue_refund(
    conn: sqlite3.Connection,
    order_id: int,
    amount: float,
    *,
    _today: date | None = None,
) -> dict:
    """Issue a refund.  The guard re-checks rules 2-5 in code."""
    if _guard_enabled():
        order = db.get_order(conn, order_id)
        if order is None:
            return _block("2", "Order not found.")
        if order["status"] != "delivered":
            return _block("2", f"Order status is '{order['status']}', must be 'delivered'.")

        # 30-day window
        today = _get_today(_today)
        delivered = date.fromisoformat(order["delivered_at"])
        if (today - delivered).days > 30:
            return _block("2", f"Delivery was {(today - delivered).days} days ago (limit 30).")

        if order["is_final_sale"]:
            return _block("3", "Final-sale items cannot be refunded.")

        if amount > 5000:
            return _block("4", f"Amount ₹{amount:.0f} exceeds ₹5,000; must escalate.")

        if db.get_refunds_for_order(conn, order_id):
            return _block("5", "A refund already exists for this order.")

    refund = db.create_refund(conn, order_id, amount)
    return {"ok": True, "refund_id": refund["id"], "amount": refund["amount"]}


def cancel_order(
    conn: sqlite3.Connection, order_id: int
) -> dict:
    """Cancel an order.  Guard enforces rule 6: processing only."""
    if _guard_enabled():
        order = db.get_order(conn, order_id)
        if order is None:
            return _block("6", "Order not found.")
        if order["status"] != "processing":
            return _block(
                "6",
                f"Order status is '{order['status']}', must be 'processing' to cancel.",
            )

    order = db.cancel_order(conn, order_id)
    return {"ok": True, "order_id": order["id"], "status": order["status"]}


def update_address(
    conn: sqlite3.Connection, order_id: int, new_address: str
) -> dict:
    """Change shipping address.  Guard enforces rule 7: processing only."""
    if _guard_enabled():
        order = db.get_order(conn, order_id)
        if order is None:
            return _block("7", "Order not found.")
        if order["status"] != "processing":
            return _block(
                "7",
                f"Order status is '{order['status']}', must be 'processing' to change address.",
            )

    order = db.update_order_address(conn, order_id, new_address)
    return {"ok": True, "order_id": order["id"], "address": order["address"]}


def escalate_to_human(
    conn: sqlite3.Connection, reason: str, order_id: int | None = None
) -> dict:
    """Escalate to a human supervisor.  No guard — escalation is always OK."""
    esc = db.create_escalation(conn, order_id, reason)
    return {"ok": True, "escalation_id": esc["id"], "reason": esc["reason"]}


# ── JSON schemas for the LLM ─────────────────────────────────────────
# OpenAI-compatible format.  llm.py can adapt these for other providers.

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "lookup_order",
            "description": (
                "Look up an order by ID and verify the customer's email address. "
                "Must be called FIRST before any other action (policy rule 1)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "integer",
                        "description": "The order ID the customer provided.",
                    },
                    "email": {
                        "type": "string",
                        "description": "The email address the customer provided for verification.",
                    },
                },
                "required": ["order_id", "email"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_refund_eligibility",
            "description": (
                "Check whether an order is eligible for a refund. "
                "Call this BEFORE issue_refund to verify all policy rules."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "integer",
                        "description": "The order ID to check.",
                    },
                },
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "issue_refund",
            "description": (
                "Issue a refund for an order. Only call after check_refund_eligibility "
                "confirms the order is eligible. Refunds above Rs 5,000 must be "
                "escalated instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "integer",
                        "description": "The order ID to refund.",
                    },
                    "amount": {
                        "type": "number",
                        "description": "Refund amount in rupees (must be ≤ Rs 5,000).",
                    },
                },
                "required": ["order_id", "amount"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_order",
            "description": (
                "Cancel an order. Only allowed when order status is 'processing'. "
                "Shipped or delivered orders cannot be cancelled."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "integer",
                        "description": "The order ID to cancel.",
                    },
                },
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_address",
            "description": (
                "Update the shipping address for an order. "
                "Only allowed when order status is 'processing'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "integer",
                        "description": "The order ID to update.",
                    },
                    "new_address": {
                        "type": "string",
                        "description": "The new shipping address.",
                    },
                },
                "required": ["order_id", "new_address"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "escalate_to_human",
            "description": (
                "Escalate the issue to a human supervisor. Use for: "
                "refunds above Rs 5,000, abusive language, legal/fraud threats, "
                "or any request the policy does not cover."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "integer",
                        "description": "The order ID (if applicable).",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why this needs human review.",
                    },
                },
                "required": ["reason"],
            },
        },
    },
]


# ── Registry & dispatch ──────────────────────────────────────────────

TOOL_REGISTRY: dict[str, callable] = {
    "lookup_order": lookup_order,
    "check_refund_eligibility": check_refund_eligibility,
    "issue_refund": issue_refund,
    "cancel_order": cancel_order,
    "update_address": update_address,
    "escalate_to_human": escalate_to_human,
}


def dispatch(conn: sqlite3.Connection, tool_name: str, arguments: dict) -> dict:
    """Route a tool call from the LLM to the right Python function.

    The agent loop calls this; it never touches tool functions directly.
    """
    fn = TOOL_REGISTRY.get(tool_name)
    if fn is None:
        return {"ok": False, "error": f"Unknown tool: {tool_name}"}
    return fn(conn, **arguments)
