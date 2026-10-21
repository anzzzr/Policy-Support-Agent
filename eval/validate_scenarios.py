"""
Scenario Schema and Seed Referential Integrity Validator.

Validates that all scenario YAML files conform to schema and reference
valid database entities in the seed database.
Checks both 'scenarios/' and 'scenarios_heldout/' without leaking held-out content.
"""

import sys
from pathlib import Path
import yaml
from app.db import get_connection

ROOT_DIR = Path(__file__).resolve().parent.parent

REQUIRED_FIELDS = {"id", "category", "user_turns", "expected_db", "should_escalate"}


def validate_scenario_file(
    fpath: Path, valid_customers: set[int], valid_orders: set[int]
) -> list[str]:
    errors = []
    try:
        with open(fpath, "r", encoding="utf-8") as fp:
            data = yaml.safe_load(fp)
    except Exception as e:
        return [f"Failed to parse YAML: {e}"]

    if not isinstance(data, dict):
        return ["Root element must be a mapping/dict."]

    for req in REQUIRED_FIELDS:
        if req not in data:
            errors.append(f"Missing required field: '{req}'")

    if "id" in data and not isinstance(data["id"], str):
        errors.append("Field 'id' must be a string.")

    if "category" in data and not isinstance(data["category"], str):
        errors.append("Field 'category' must be a string.")

    if "should_escalate" in data and not isinstance(data["should_escalate"], bool):
        errors.append("Field 'should_escalate' must be a boolean.")

    if "user_turns" in data:
        if not isinstance(data["user_turns"], list) or len(data["user_turns"]) == 0:
            errors.append("Field 'user_turns' must be a non-empty list of strings.")
        elif not all(isinstance(turn, str) for turn in data["user_turns"]):
            errors.append("All items in 'user_turns' must be strings.")

    if "expected_db" in data:
        edb = data["expected_db"]
        if not isinstance(edb, dict):
            errors.append("Field 'expected_db' must be a dictionary.")
        else:
            if "orders" in edb and not isinstance(edb["orders"], list):
                errors.append("Field 'expected_db.orders' must be a list.")
            elif "orders" in edb:
                for o in edb["orders"]:
                    if not isinstance(o, dict) or "id" not in o:
                        errors.append(
                            "Each item in 'expected_db.orders' must be a dict with 'id'."
                        )
                    elif o["id"] not in valid_orders:
                        errors.append(
                            f"Referenced order {o['id']} in 'expected_db.orders' does not exist in seed."
                        )

            if "refunds" in edb and not isinstance(edb["refunds"], list):
                errors.append("Field 'expected_db.refunds' must be a list.")
            elif "refunds" in edb:
                for r in edb["refunds"]:
                    if isinstance(r, dict) and "order_id" in r:
                        if r["order_id"] not in valid_orders:
                            errors.append(
                                f"Referenced order {r['order_id']} in 'expected_db.refunds' does not exist in seed."
                            )

    # Seed referential integrity
    seed_block = data.get("seed")
    if seed_block and isinstance(seed_block, dict):
        for cid in seed_block.get("customers", []):
            if cid not in valid_customers:
                errors.append(
                    f"Referenced customer {cid} in 'seed.customers' does not exist in seed."
                )
        for oid in seed_block.get("orders", []):
            if oid not in valid_orders:
                errors.append(
                    f"Referenced order {oid} in 'seed.orders' does not exist in seed."
                )

    # Expected escalation order_id
    exp_esc = data.get("expected_escalation")
    if exp_esc and isinstance(exp_esc, dict):
        e_oid = exp_esc.get("order_id")
        if isinstance(e_oid, int) and e_oid not in valid_orders:
            errors.append(
                f"Referenced order {e_oid} in 'expected_escalation.order_id' does not exist in seed."
            )
        elif isinstance(e_oid, list):
            for oid_item in e_oid:
                if isinstance(oid_item, int) and oid_item not in valid_orders:
                    errors.append(
                        f"Referenced order {oid_item} in 'expected_escalation.order_id' does not exist in seed."
                    )

    return errors


def validate_directory(
    dir_path: Path, valid_customers: set[int], valid_orders: set[int]
) -> bool:
    if not dir_path.exists():
        return True

    files = sorted(dir_path.glob("*.yaml"))
    if not files:
        print(f"[{dir_path.name}] No YAML scenario files found.")
        return True

    print(f"Validating {dir_path.name}/ ({len(files)} scenarios)...")
    all_ok = True
    for f in files:
        errs = validate_scenario_file(f, valid_customers, valid_orders)
        if errs:
            all_ok = False
            # Only print scenario filename and error descriptions, never turn contents
            print(f"  FAILED: {f.name}")
            for err in errs:
                print(f"    - {err}")

    if all_ok:
        print(f"  All {len(files)} scenarios in {dir_path.name}/ passed validation.")
    return all_ok


def main():
    conn = get_connection()
    try:
        valid_customers = {
            row["id"] for row in conn.execute("SELECT id FROM customers").fetchall()
        }
        valid_orders = {
            row["id"] for row in conn.execute("SELECT id FROM orders").fetchall()
        }
    finally:
        conn.close()

    scenarios_dir = ROOT_DIR / "scenarios"
    heldout_dir = ROOT_DIR / "scenarios_heldout"

    ok_scenarios = validate_directory(scenarios_dir, valid_customers, valid_orders)
    ok_heldout = validate_directory(heldout_dir, valid_customers, valid_orders)

    if not ok_scenarios or not ok_heldout:
        print("\n[FAILED] Scenario validation failed.")
        sys.exit(1)
    else:
        print("\n[SUCCESS] All scenario files are valid and reference existing seed data.")


if __name__ == "__main__":
    main()
