"""Independent audit of output/ against the raw CSVs.

Deliberately shares NO code with src/: it re-reads the CSVs with plain stdlib
and recomputes every graded number from scratch, then diffs against what the
pipeline wrote. A bug that lives in src/ cannot hide from this, because this
file never imports it.

    python tools/audit.py

Not part of the submission zip -- README section 9 says only output/ ships.
"""

from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FMT = "%Y-%m-%d %H:%M:%S"

# Optional first argument: which output directory to audit (default output/).
OUT_DIR = ROOT / (sys.argv[1] if len(sys.argv) > 1 else "output")


def read(name):
    with (ROOT / "data" / name).open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def ts(value):
    value = (value or "").strip()
    return datetime.strptime(value, FMT) if value else None


def main() -> int:
    orders = {r["order_id"]: r for r in read("olist_orders_dataset.csv")}
    customers = {r["customer_id"]: r for r in read("olist_customers_dataset.csv")}
    products = {r["product_id"]: r["product_category_name"].strip() for r in read("olist_products_dataset.csv")}
    sellers = {r["seller_id"] for r in read("olist_sellers_dataset.csv")}

    items = defaultdict(list)
    for r in read("olist_order_items_dataset.csv"):
        items[r["order_id"]].append(r)
    pays = defaultdict(list)
    for r in read("olist_order_payments_dataset.csv"):
        pays[r["order_id"]].append(r)

    unique_orders = defaultdict(list)
    for idx, (oid, o) in enumerate(orders.items()):
        cust = customers.get(o["customer_id"])
        if cust:
            unique_orders[cust["customer_unique_id"]].append((idx, oid))

    problems: list[str] = []
    checked = 0

    for path in sorted((ROOT / "input").glob("EC_*.json")):
        case = json.loads(path.read_text(encoding="utf-8"))
        cid = case["case_id"]
        oid = case["customer_request"]["claimed_order_id"]

        out_path = OUT_DIR / f"{cid}.json"
        if not out_path.exists():
            problems.append(f"{cid}: output file missing")
            continue
        doc = json.loads(out_path.read_text(encoding="utf-8"))
        checked += 1

        def bad(msg):
            problems.append(f"{cid}: {msg}")

        o = orders[oid]
        it = sorted(items.get(oid, []), key=lambda r: int(r["order_item_id"]))
        pm = sorted(pays.get(oid, []), key=lambda r: int(r["payment_sequential"]))

        # --- money ------------------------------------------------------
        item_total = round(sum(float(r["price"]) for r in it), 2)
        freight_total = round(sum(float(r["freight_value"]) for r in it), 2)
        pay_total = round(sum(float(r["payment_value"]) for r in pm), 2)
        rec = doc["payment_reconciliation"]
        if rec["item_total_brl"] != item_total:
            bad(f"item_total {rec['item_total_brl']} != {item_total}")
        if rec["freight_total_brl"] != freight_total:
            bad(f"freight_total {rec['freight_total_brl']} != {freight_total}")
        if rec["payment_total_brl"] != pay_total:
            bad(f"payment_total {rec['payment_total_brl']} != {pay_total}")

        if it:
            expected = round(item_total + freight_total, 2)
            diff = round(pay_total - expected, 2)
            if rec["expected_total_brl"] != expected:
                bad(f"expected_total {rec['expected_total_brl']} != {expected}")
            if rec["difference_brl"] != diff:
                bad(f"difference {rec['difference_brl']} != {diff}")
            if rec["reconciled"] != (abs(diff) <= 0.10):
                bad("reconciled flag wrong")
        else:
            for key in ("expected_total_brl", "difference_brl", "reconciled"):
                if rec[key] is not None:
                    bad(f"{key} must be null with no item rows")

        # --- delivery ---------------------------------------------------
        dl = doc["delivery_analysis"]
        delivered, estimated = ts(o["order_delivered_customer_date"]), ts(o["order_estimated_delivery_date"])
        carrier = ts(o["order_delivered_carrier_date"])
        variance = round((delivered - estimated).total_seconds() / 3600, 2) if delivered and estimated else None
        if dl["delivery_variance_hours"] != variance:
            bad(f"delivery_variance {dl['delivery_variance_hours']} != {variance}")
        if dl["delivered_at"] != (o["order_delivered_customer_date"].strip() or None):
            bad("delivered_at mismatch")
        if dl["carrier_handoff_at"] != (o["order_delivered_carrier_date"].strip() or None):
            bad("carrier_handoff_at mismatch")

        expected_late = []
        seen_sellers = []
        for r in it:
            if r["seller_id"] not in seen_sellers:
                seen_sellers.append(r["seller_id"])
        for sid in seen_sellers:
            limits = [ts(r["shipping_limit_date"]) for r in it if r["seller_id"] == sid]
            limits = [x for x in limits if x]
            earliest = min(limits) if limits else None
            hv = round((carrier - earliest).total_seconds() / 3600, 2) if carrier and earliest else None
            if hv is not None and hv > 0:
                expected_late.append(sid)
        if dl["late_handoff_seller_ids"] != expected_late:
            bad(f"late sellers {dl['late_handoff_seller_ids']} != {expected_late}")

        # --- verdict ----------------------------------------------------
        status = o["order_status"].strip()
        late = bool(delivered and estimated and variance and variance > 0)
        reconciled = abs(round(pay_total - round(item_total + freight_total, 2), 2)) <= 0.10 if it else None
        if status == "canceled" and pay_total > 0:
            want, refund = "canceled_order_paid", pay_total
        elif status == "unavailable" and pay_total > 0:
            want, refund = "unavailable_order_paid", pay_total
        elif late and expected_late:
            want, refund = "late_delivery_seller", freight_total
        elif late:
            want, refund = "late_delivery_logistics", freight_total
        elif len(pm) >= 2 and reconciled:
            want, refund = "valid_split_payment", 0.0
        else:
            want, refund = "unsupported_late_claim", 0.0

        got = doc["case_assessment"]["primary_issue"]
        if got != want:
            bad(f"primary_issue {got} != {want}")
        if doc["financial_resolution"]["recommended_refund_brl"] != refund:
            bad(f"refund {doc['financial_resolution']['recommended_refund_brl']} != {refund}")
        want_status = "action_required" if refund > 0 else "no_action"
        if doc["case_assessment"]["case_status"] != want_status:
            bad(f"case_status != {want_status}")

        # --- secondary issues -------------------------------------------
        cats, pids = [], []
        for r in it:
            if r["product_id"] not in pids:
                pids.append(r["product_id"])
        for pid in pids:
            c = products.get(pid, "")
            if c and c not in cats:
                cats.append(c)
        uid = customers[o["customer_id"]]["customer_unique_id"]
        related = [x for _, x in sorted(unique_orders[uid]) if x != oid]
        want_secondary = []
        if len(it) >= 2:
            want_secondary.append("multi_item_order")
        if len(seen_sellers) >= 2:
            want_secondary.append("multi_seller_order")
        if len(pm) >= 2:
            want_secondary.append("split_payment")
        if related:
            want_secondary.append("repeat_customer")
        if len(cats) >= 2:
            want_secondary.append("multiple_categories")
        if doc["case_assessment"]["secondary_issues"] != want_secondary:
            bad(f"secondary {doc['case_assessment']['secondary_issues']} != {want_secondary}")

        # --- entities and context ---------------------------------------
        ent = doc["affected_entities"]
        if ent["order_ids"] != [oid]:
            bad("order_ids must hold exactly the claimed order")
        if ent["item_ids"] != [f"{oid}:{r['order_item_id']}" for r in it][:5]:
            bad("item_ids mismatch")
        if ent["payment_ids"] != [f"{oid}:{r['payment_sequential']}" for r in pm][:5]:
            bad("payment_ids mismatch")
        if ent["seller_ids"] != seen_sellers[:3]:
            bad("seller_ids mismatch")
        cc = doc["customer_context"]
        if cc["customer_unique_id"] != uid:
            bad("customer_unique_id mismatch")
        if cc["related_order_ids"] != related[:5]:
            bad(f"related_order_ids mismatch ({len(cc['related_order_ids'])} vs {len(related[:5])})")
        if set(cc["related_order_ids"]) & set(ent["order_ids"]):
            bad("related order leaked into affected_entities")
        pc = doc["product_context"]
        if pc["product_ids"] != pids[:5]:
            bad("product_ids mismatch")
        if pc["category_names"] != cats[:5]:
            bad("category_names mismatch")

        # --- evidence ----------------------------------------------------
        for ref in doc["evidence_ids"]:
            kind, _, rest = ref.partition(":")
            if kind == "order" and rest not in orders:
                bad(f"evidence order not in CSV: {ref}")
            elif kind == "item" and rest not in {f"{oid}:{r['order_item_id']}" for r in it}:
                bad(f"evidence item not in CSV: {ref}")
            elif kind == "payment" and rest not in {f"{oid}:{r['payment_sequential']}" for r in pm}:
                bad(f"evidence payment not in CSV: {ref}")
            elif kind == "seller" and rest not in sellers:
                bad(f"evidence seller not in CSV: {ref}")
            elif kind not in ("order", "item", "payment", "seller", "policy"):
                bad(f"bad evidence prefix: {ref}")

        conf = doc["case_assessment"]["confidence"]
        if not (0.0 <= conf <= 1.0):
            bad(f"confidence out of range: {conf}")

    print(f"audited {checked} case(s) in {OUT_DIR.name}/ against the raw CSVs")
    if problems:
        print(f"FAIL -- {len(problems)} problem(s):")
        for p in problems[:40]:
            print("   ", p)
        return 1
    print("PASS -- every graded value recomputes to the same answer")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
