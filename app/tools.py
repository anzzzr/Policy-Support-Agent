"""
Tool definitions, JSON schemas for the LLM, and policy guards.

Each tool is a plain function: (conn, **llm_args) -> dict.
Write-tools have a guard that re-checks policy rules in Python so even if
the LLM hallucinates a bad call, the database stays correct.

Set GUARD=off (env var) to disable guards for evaluation ablation only.
"""

import inspect
import logging
import math
import os
import re
import sqlite3
from datetime import date

from app import db

log = logging.getLogger(__name__)


# ── Guard helpers ─────────────────────────────────────────────────────

def _get_today(_override: date | None = None) -> date:
    """Return effective date for 30-day policy checks."""
    if _override:
        return _override
    env_date = os.environ.get("EVAL_TODAY") or os.environ.get("MOCK_DATE")
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
    return {
        "ok": False,
        "success": False,
        "error": f"Policy violation (rule {rule}): {detail}",
    }


def _init_session(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TEMP TABLE IF NOT EXISTS _session_verified_orders (order_id INTEGER PRIMARY KEY)"
    )


def mark_order_verified(conn: sqlite3.Connection, order_id: int) -> None:
    """Mark an order as identity-verified in the current session."""
    _init_session(conn)
    conn.execute(
        "INSERT OR IGNORE INTO _session_verified_orders (order_id) VALUES (?)",
        (order_id,),
    )


def is_order_verified(conn: sqlite3.Connection, order_id: int) -> bool:
    """Return True if the order has been verified with matching email in this session."""
    _init_session(conn)
    row = conn.execute(
        "SELECT 1 FROM _session_verified_orders WHERE order_id = ?",
        (order_id,),
    ).fetchone()
    return row is not None


def clear_session_verifications(conn: sqlite3.Connection) -> None:
    """Reset verified orders for the connection session."""
    _init_session(conn)
    conn.execute("DELETE FROM _session_verified_orders")


def _validate_order_id(order_id: any) -> tuple[bool, str | None]:
    if isinstance(order_id, bool) or not isinstance(order_id, int) or order_id <= 0:
        return False, "Invalid order_id: must be a positive integer."
    return True, None


def _validate_email(email: any) -> tuple[bool, str | None, str]:
    if not isinstance(email, str):
        return False, "Invalid email: must be a string.", ""
    email_clean = email.strip().casefold()
    if not email_clean or "@" not in email_clean or len(email_clean) > 254:
        return False, "Invalid email: must be a valid email string.", ""
    return True, None, email_clean


def _validate_address(address: any) -> tuple[bool, str | None, str]:
    if not isinstance(address, str):
        return False, "Invalid address: must be a string.", ""
    if "\n" in address or "\r" in address:
        return False, "Invalid address: address cannot contain newlines.", ""
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", address).strip()
    if not cleaned:
        return False, "Invalid address: address cannot be empty.", ""
    if len(cleaned) > 300:
        return False, "Invalid address: exceeds maximum length of 300 characters.", ""
    return True, None, cleaned


def _validate_reason(reason: any) -> tuple[bool, str | None, str]:
    if not isinstance(reason, str):
        return False, "Invalid reason: must be a string.", ""
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", reason).strip()
    if not cleaned:
        return False, "Invalid reason: reason cannot be empty.", ""
    if len(cleaned) > 500:
        return False, "Invalid reason: exceeds maximum length of 500 characters.", ""
    return True, None, cleaned


# ── Read tools (no guard needed) ─────────────────────────────────────

def lookup_order(
    conn: sqlite3.Connection, order_id: int, email: str
) -> dict:
    """Identity verification (policy rule 1).

    Returns order details only if the email matches the customer on file.
    """
    valid_id, err_id = _validate_order_id(order_id)
    if not valid_id:
        return {"ok": False, "success": False, "error": err_id}

    valid_email, err_email, email_clean = _validate_email(email)
    if not valid_email:
        return {"ok": False, "success": False, "error": err_email}

    order = db.lookup_order(conn, order_id, email_clean)
    if order is None:
        return {"ok": False, "success": False, "error": "Order details do not match our records."}

    # Rule 1 passed: mark order verified for the duration of this session
    mark_order_verified(conn, order_id)

    customer = db.get_customer(conn, order["customer_id"])
    return {
        "ok": True,
        "success": True,
        "order_id": order["id"],
        "status": order["status"],
        "total": order["total"],
        "address": order["address"],
        "delivered_at": order["delivered_at"],
        "is_final_sale": bool(order["is_final_sale"]),
        "customer_name": customer["name"] if customer else "",
    }


def check_refund_eligibility(
    conn: sqlite3.Connection, order_id: int, *, _today: date | None = None
) -> dict:
    """Check all refund rules (2-5) without changing anything.

    _today is injectable for deterministic tests; defaults to today.
    """
    valid_id, err_id = _validate_order_id(order_id)
    if not valid_id:
        return {
            "ok": False,
            "success": False,
            "eligible": False,
            "reason": err_id,
            "error": err_id,
        }

    # Rule 1: identity verification required in session
    if _guard_enabled() and not is_order_verified(conn, order_id):
        res = _block(
            "1",
            f"Order {order_id} has not been verified in this session. Call lookup_order with customer email first.",
        )
        res["eligible"] = False
        res["reason"] = res["error"]
        return res

    order = db.get_order(conn, order_id)
    if order is None:
        return {
            "ok": False,
            "success": False,
            "eligible": False,
            "reason": "Order not found.",
            "error": "Order not found.",
        }

    # Rule 2a: must be delivered
    if order["status"] != "delivered":
        return {
            "ok": True,
            "success": True,
            "eligible": False,
            "reason": f"Order status is '{order['status']}', not 'delivered'.",
        }

    # Rule 3: no final-sale refunds
    if order["is_final_sale"]:
        return {
            "ok": True,
            "success": True,
            "eligible": False,
            "reason": "Final-sale items cannot be refunded.",
        }

    # Rule 2b: within 30 days of delivery
    today = _get_today(_today)
    delivered = date.fromisoformat(order["delivered_at"])
    days_since = (today - delivered).days
    if days_since > 30:
        return {
            "ok": True,
            "success": True,
            "eligible": False,
            "reason": f"Delivery was {days_since} days ago (limit is 30).",
        }

    # Rule 5: one refund per order
    if db.get_refunds_for_order(conn, order_id):
        return {
            "ok": True,
            "success": True,
            "eligible": False,
            "reason": "A refund has already been issued for this order.",
        }

    # Rule 4: >5000 must be escalated
    if order["total"] > 5000:
        return {
            "ok": True,
            "success": True,
            "eligible": False,
            "reason": (
                f"Refund amount ₹{order['total']:.0f} exceeds ₹5,000. "
                "Must be escalated to a human."
            ),
        }

    return {
        "ok": True,
        "success": True,
        "eligible": True,
        "reason": "Eligible for refund.",
        "max_amount": order["total"],
    }


# ── Write tools (guarded) ────────────────────────────────────────────

def issue_refund(
    conn: sqlite3.Connection,
    order_id: int,
    *,
    _today: date | None = None,
) -> dict:
    """Issue a full refund for an order. The guard re-checks rules 1-5 in code.

    Validation on order_id runs even when GUARD=off. Policy rules (Rule 1 verification,
    Rule 2 30-day/delivered, Rule 3 final-sale, Rule 4 limit <= 5000, Rule 5 single refund)
    are enforced when GUARD=on. Always refunds the full order total.
    """
    valid_id, err_id = _validate_order_id(order_id)
    if not valid_id:
        return {"ok": False, "success": False, "error": err_id}

    order = db.get_order(conn, order_id)

    # Policy guard enforcement (active when GUARD=on)
    if _guard_enabled():
        # Existence and verification check runs FIRST: identical error for nonexistent and unverified
        if order is None or not is_order_verified(conn, order_id):
            return _block(
                "1",
                f"Order {order_id} has not been verified in this session. Call lookup_order with customer email first.",
            )

        if order["status"] != "delivered":
            return _block("2", f"Order status is '{order['status']}', must be 'delivered'.")

        # 30-day window
        today = _get_today(_today)
        delivered = date.fromisoformat(order["delivered_at"])
        if (today - delivered).days > 30:
            return _block("2", f"Delivery was {(today - delivered).days} days ago (limit 30).")

        if order["is_final_sale"]:
            return _block("3", "Final-sale items cannot be refunded.")

        total_paise = round(order["total"] * 100)
        if total_paise > 500000:
            return _block("4", "Amount exceeds ₹5,000; must escalate.")

        if db.get_refunds_for_order(conn, order_id):
            return _block("5", "A refund already exists for this order.")
    else:
        if order is None:
            return {"ok": False, "success": False, "error": "Order not found."}

    refund = db.create_refund(conn, order_id, order["total"])
    return {
        "ok": True,
        "success": True,
        "refund_id": refund["id"],
        "amount": refund["amount"],
    }


def cancel_order(
    conn: sqlite3.Connection, order_id: int
) -> dict:
    """Cancel an order.  Guard enforces rule 6: processing only."""
    valid_id, err_id = _validate_order_id(order_id)
    if not valid_id:
        return {"ok": False, "success": False, "error": err_id}

    if _guard_enabled():
        if not is_order_verified(conn, order_id):
            return _block(
                "1",
                f"Order {order_id} has not been verified in this session. Call lookup_order with customer email first.",
            )

        order = db.get_order(conn, order_id)
        if order is None:
            return _block("6", "Order not found.")
        if order["status"] != "processing":
            return _block(
                "6",
                f"Order status is '{order['status']}', must be 'processing' to cancel.",
            )
    else:
        order = db.get_order(conn, order_id)
        if order is None:
            return {"ok": False, "success": False, "error": "Order not found."}

    order = db.cancel_order(conn, order_id)
    return {
        "ok": True,
        "success": True,
        "order_id": order["id"],
        "status": order["status"],
    }


def update_address(
    conn: sqlite3.Connection, order_id: int, new_address: str
) -> dict:
    """Change shipping address.  Guard enforces rule 7: processing only."""
    valid_id, err_id = _validate_order_id(order_id)
    if not valid_id:
        return {"ok": False, "success": False, "error": err_id}

    valid_addr, err_addr, cleaned_address = _validate_address(new_address)
    if not valid_addr:
        return {"ok": False, "success": False, "error": err_addr}

    if _guard_enabled():
        if not is_order_verified(conn, order_id):
            return _block(
                "1",
                f"Order {order_id} has not been verified in this session. Call lookup_order with customer email first.",
            )

        order = db.get_order(conn, order_id)
        if order is None:
            return _block("7", "Order not found.")
        if order["status"] != "processing":
            return _block(
                "7",
                f"Order status is '{order['status']}', must be 'processing' to change address.",
            )
    else:
        order = db.get_order(conn, order_id)
        if order is None:
            return {"ok": False, "success": False, "error": "Order not found."}

    order = db.update_order_address(conn, order_id, cleaned_address)
    return {
        "ok": True,
        "success": True,
        "order_id": order["id"],
        "address": order["address"],
    }


VALID_REASON_CODES = {
    "abuse",
    "legal_or_fraud",
    "out_of_scope",
    "refund_over_limit",
    "customer_request",
    "insistence",
}


def escalate_to_human(
    conn: sqlite3.Connection,
    reason_code: str,
    note: str = "",
    order_id: int | None = None,
) -> dict:
    """Escalate to a human supervisor.  No guard — escalation is always OK."""
    if order_id is not None:
        valid_id, err_id = _validate_order_id(order_id)
        if not valid_id:
            return {"ok": False, "success": False, "error": err_id}

    if not isinstance(reason_code, str) or not reason_code.strip():
        return {"ok": False, "success": False, "error": "Reason code cannot be empty."}

    if reason_code not in VALID_REASON_CODES:
        return {
            "ok": False,
            "success": False,
            "error": f"Invalid reason_code: '{reason_code}'. Must be one of {sorted(VALID_REASON_CODES)}.",
        }

    if not isinstance(note, str):
        note = str(note)
    cleaned_note = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", note).strip()
    if len(cleaned_note) > 500:
        return {"ok": False, "success": False, "error": "Invalid note: exceeds maximum length of 500 characters."}

    stored_reason = f"[{reason_code}] {cleaned_note}".strip() if cleaned_note else f"[{reason_code}]"

    esc = db.create_escalation(conn, order_id, stored_reason)
    return {
        "ok": True,
        "success": True,
        "escalation_id": esc["id"],
        "reason_code": reason_code,
        "note": cleaned_note,
        "reason": esc["reason"],
    }


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
                "Issue a full refund for an order. Only call after check_refund_eligibility "
                "confirms the order is eligible. Refunds above Rs 5,000 must be "
                "escalated instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "integer",
                        "description": "The order ID to refund (always refunds the full order total).",
                    },
                },
                "required": ["order_id"],
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
                "Escalate the issue to a human supervisor. Provide an enumerated reason_code "
                "and an optional free-text note."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "integer",
                        "description": "The order ID (if applicable, else null).",
                    },
                    "reason_code": {
                        "type": "string",
                        "enum": [
                            "abuse",
                            "legal_or_fraud",
                            "out_of_scope",
                            "refund_over_limit",
                            "customer_request",
                            "insistence",
                        ],
                        "description": "The categorized reason code for escalation.",
                    },
                    "note": {
                        "type": "string",
                        "description": "Free-text note explaining the context.",
                    },
                },
                "required": ["reason_code"],
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
    Catches missing, extra, and wrong-type arguments returning a clean error dict.
    """
    if not isinstance(arguments, dict):
        return {
            "ok": False,
            "success": False,
            "error": f"Tool arguments for '{tool_name}' must be a dictionary/object.",
        }

    fn = TOOL_REGISTRY.get(tool_name)
    if fn is None:
        return {
            "ok": False,
            "success": False,
            "error": f"Unknown tool: '{tool_name}'",
        }

    sig = inspect.signature(fn)
    allowed_params = set(sig.parameters.keys()) - {"conn"}
    public_params = {p for p in allowed_params if not p.startswith("_")}
    has_kwargs = any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )

    if not has_kwargs:
        extra_args = set(arguments.keys()) - allowed_params
        if extra_args:
            return {
                "ok": False,
                "success": False,
                "error": (
                    f"Unexpected argument(s) for '{tool_name}': "
                    f"{', '.join(sorted(extra_args))}. Allowed arguments: {', '.join(sorted(public_params))}"
                ),
            }

    required_args = [
        name
        for name, p in sig.parameters.items()
        if name != "conn"
        and not name.startswith("_")
        and p.default is inspect.Parameter.empty
        and p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    ]
    missing_args = [arg for arg in required_args if arg not in arguments]
    if missing_args:
        return {
            "ok": False,
            "success": False,
            "error": (
                f"Missing required argument(s) for '{tool_name}': "
                f"{', '.join(sorted(missing_args))}"
            ),
        }

    try:
        return fn(conn, **arguments)
    except TypeError as e:
        return {
            "ok": False,
            "success": False,
            "error": f"Invalid arguments for '{tool_name}': {e}",
        }
    except Exception as e:
        log.exception("Unexpected exception in tool %s", tool_name)
        return {
            "ok": False,
            "success": False,
            "error": f"Execution error in '{tool_name}': {e}",
        }
