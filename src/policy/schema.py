"""Output schema: assembly, array limits and hard-gate validation.

README section 8 says a hard-gated case scores 0, but never enumerates the
gates. We treat every structural promise the README does make as a gate --
file/case_id match, the enumerations, the array caps, the confidence range, the
null-handling rule and evidence-ID well-formedness -- and fail loudly rather
than shipping a case that a strict grader would zero.
"""

from __future__ import annotations

import math
import re
from typing import Any, Callable

TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
ID_RE = re.compile(r"^[0-9a-f]{32}$")

LIMITS = {
    "order_ids": 5,
    "item_ids": 5,
    "seller_ids": 3,
    "payment_ids": 5,
    "related_order_ids": 5,
    "product_ids": 5,
    "category_names": 5,
    "ranked_causes": 3,
    "responsible_parties": 3,
    "evidence_ids": 20,
    "resolution_actions": 5,
}

VALID_PRIMARY = set(
    [
        "canceled_order_paid",
        "unavailable_order_paid",
        "late_delivery_seller",
        "late_delivery_logistics",
        "valid_split_payment",
        "unsupported_late_claim",
    ]
)

VALID_SECONDARY = set(
    [
        "multi_item_order",
        "multi_seller_order",
        "split_payment",
        "repeat_customer",
        "multiple_categories",
    ]
)

VALID_ROOT_CAUSES = set(
    [
        "SELLER_HANDOFF_AFTER_LIMIT",
        "CARRIER_DELIVERED_AFTER_ESTIMATE",
        "ORDER_CANCELED_AFTER_PAYMENT",
        "ORDER_UNAVAILABLE_AFTER_PAYMENT",
        "MULTIPLE_PAYMENTS_RECONCILED",
        "DELIVERY_WITHIN_ESTIMATE",
    ]
)

VALID_ACTIONS = set(
    [
        "issue_full_refund",
        "refund_freight",
        "explain_valid_split_payment",
        "reject_late_refund",
        "review_seller_handoff",
        "review_carrier_delay",
        "verify_refund_completion",
        "coordinate_multi_seller_case",
        "verify_payment_allocation",
    ]
)

VALID_STATUS = {"action_required", "no_action"}
VALID_PARTY_TYPES = {"platform", "seller", "logistics_provider"}


def cap(values: list, key: str) -> list:
    """Apply the README array cap, preserving source order."""
    return list(values)[: LIMITS[key]]


def _is_ts(value: Any) -> bool:
    return value is None or (isinstance(value, str) and bool(TS_RE.match(value)))


def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _round2(value: Any) -> Any:
    return round(value, 2) if _is_num(value) else value


def validate(
    doc: dict,
    *,
    expected_case_id: str,
    order_exists: Callable[[str], bool],
    seller_exists: Callable[[str], bool],
    known_item_refs: set[str],
    known_payment_refs: set[str],
) -> list[str]:
    """Return a list of gate violations. Empty list == safe to write."""
    errors: list[str] = []

    def need(cond: bool, msg: str) -> None:
        if not cond:
            errors.append(msg)

    # -- no negative zero anywhere ----------------------------------------
    # json.dump writes -0.0 literally, which is a different token from 0.0 to
    # any grader comparing text or doing a strict check. Gate it at the door.
    def _scan_neg_zero(node, path="") -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                _scan_neg_zero(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                _scan_neg_zero(v, f"{path}[{i}]")
        elif isinstance(node, float) and node == 0.0 and math.copysign(1.0, node) < 0:
            errors.append(f"negative zero at {path.lstrip('.')}")

    _scan_neg_zero(doc)

    # -- top level ---------------------------------------------------------
    for key in (
        "case_id",
        "case_assessment",
        "affected_entities",
        "customer_context",
        "product_context",
        "delivery_analysis",
        "payment_reconciliation",
        "root_cause_analysis",
        "evidence_ids",
        "financial_resolution",
        "resolution_actions",
    ):
        need(key in doc, f"missing top-level key '{key}'")
    if errors:
        return errors

    need(doc["case_id"] == expected_case_id, f"case_id mismatch: {doc['case_id']!r}")

    # -- assessment --------------------------------------------------------
    a = doc["case_assessment"]
    need(a.get("primary_issue") in VALID_PRIMARY, f"bad primary_issue {a.get('primary_issue')!r}")
    need(
        isinstance(a.get("secondary_issues"), list)
        and all(s in VALID_SECONDARY for s in a["secondary_issues"]),
        "bad secondary_issues",
    )
    need(len(set(a.get("secondary_issues", []))) == len(a.get("secondary_issues", [])),
         "duplicate secondary_issues")
    need(a.get("case_status") in VALID_STATUS, f"bad case_status {a.get('case_status')!r}")
    conf = a.get("confidence")
    need(_is_num(conf) and 0.0 <= conf <= 1.0, f"confidence out of range: {conf!r}")

    # -- affected entities -------------------------------------------------
    e = doc["affected_entities"]
    for key in ("order_ids", "item_ids", "seller_ids", "payment_ids"):
        need(isinstance(e.get(key), list), f"affected_entities.{key} must be a list")
        need(len(e.get(key, [])) <= LIMITS[key], f"affected_entities.{key} exceeds cap {LIMITS[key]}")
    for oid in e.get("order_ids", []):
        need(order_exists(oid), f"unknown order_id in affected_entities: {oid}")
    for sid in e.get("seller_ids", []):
        need(seller_exists(sid), f"unknown seller_id: {sid}")
    for ref in e.get("item_ids", []):
        need(ref in known_item_refs, f"item ref not in CSV: {ref}")
    for ref in e.get("payment_ids", []):
        need(ref in known_payment_refs, f"payment ref not in CSV: {ref}")

    # -- customer / product context ---------------------------------------
    c = doc["customer_context"]
    need(isinstance(c.get("customer_unique_id"), str) and c["customer_unique_id"],
         "missing customer_unique_id")
    need(len(c.get("related_order_ids", [])) <= LIMITS["related_order_ids"],
         "related_order_ids exceeds cap 5")
    for oid in c.get("related_order_ids", []):
        need(order_exists(oid), f"unknown related order_id: {oid}")
        need(oid not in e.get("order_ids", []),
             f"related order {oid} leaked into affected_entities.order_ids")

    p = doc["product_context"]
    need(len(p.get("product_ids", [])) <= LIMITS["product_ids"], "product_ids exceeds cap 5")
    need(len(p.get("category_names", [])) <= LIMITS["category_names"],
         "category_names exceeds cap 5")

    # -- delivery ----------------------------------------------------------
    d = doc["delivery_analysis"]
    for key in ("delivered_at", "estimated_delivery_at", "carrier_handoff_at"):
        need(_is_ts(d.get(key)), f"delivery_analysis.{key} is not a CSV timestamp or null")
    need(d.get("delivery_variance_hours") is None or _is_num(d["delivery_variance_hours"]),
         "delivery_variance_hours must be a number or null")
    need(isinstance(d.get("seller_handoff_analysis"), list), "seller_handoff_analysis must be a list")
    for entry in d.get("seller_handoff_analysis", []):
        need(seller_exists(entry.get("seller_id", "")), f"unknown seller in handoff analysis: {entry}")
        need(_is_ts(entry.get("shipping_limit_at")), "bad shipping_limit_at")
        need(entry.get("handoff_variance_hours") is None or _is_num(entry["handoff_variance_hours"]),
             "bad handoff_variance_hours")
        need(isinstance(entry.get("late_handoff"), bool), "late_handoff must be a bool")
    analysed = {x.get("seller_id") for x in d.get("seller_handoff_analysis", [])}
    for sid in d.get("late_handoff_seller_ids", []):
        need(sid in analysed, f"late_handoff_seller_ids has {sid} with no handoff analysis row")

    # -- payment reconciliation -------------------------------------------
    r = doc["payment_reconciliation"]
    need(r.get("currency") == "BRL", "payment currency must be BRL")
    need(_is_num(r.get("item_total_brl")), "item_total_brl must be a number")
    need(_is_num(r.get("freight_total_brl")), "freight_total_brl must be a number")
    need(_is_num(r.get("payment_total_brl")), "payment_total_brl must be a number")
    has_items = bool(e.get("item_ids"))
    if has_items:
        need(_is_num(r.get("expected_total_brl")), "expected_total_brl must be a number")
        need(_is_num(r.get("difference_brl")), "difference_brl must be a number")
        need(isinstance(r.get("reconciled"), bool), "reconciled must be a bool")
    else:
        # README section 4: no item rows -> these three are null, arrays empty.
        for key in ("expected_total_brl", "difference_brl", "reconciled"):
            need(r.get(key) is None, f"{key} must be null when the order has no item rows")
        need(p.get("product_ids") == [], "product_ids must be empty when there are no items")
        need(p.get("category_names") == [], "category_names must be empty when there are no items")
        need(e.get("seller_ids") == [], "seller_ids must be empty when there are no items")
        need(d.get("seller_handoff_analysis") == [],
             "seller_handoff_analysis must be empty when there are no items")

    # -- root cause + evidence --------------------------------------------
    rc = doc["root_cause_analysis"]
    causes = rc.get("ranked_causes", [])
    need(0 < len(causes) <= LIMITS["ranked_causes"], "ranked_causes must hold 1..3 entries")
    for idx, cause in enumerate(causes, start=1):
        need(cause.get("cause_code") in VALID_ROOT_CAUSES, f"bad cause_code {cause.get('cause_code')!r}")
        need(cause.get("rank") == idx, f"ranked_causes rank must be 1..n in order, saw {cause.get('rank')}")
    need(len(rc.get("responsible_parties", [])) <= LIMITS["responsible_parties"],
         "responsible_parties exceeds cap 3")
    for party in rc.get("responsible_parties", []):
        need(party.get("party_type") in VALID_PARTY_TYPES, f"bad party_type {party.get('party_type')!r}")
        need(bool(party.get("party_id")), "party_id must not be empty")
        if party.get("party_type") == "seller":
            need(seller_exists(party["party_id"]), f"unknown responsible seller {party['party_id']}")

    ev = doc["evidence_ids"]
    need(isinstance(ev, list) and len(ev) <= LIMITS["evidence_ids"], "evidence_ids exceeds cap 20")
    need(len(set(ev)) == len(ev), "duplicate evidence_ids")
    for ref in ev:
        if ref.startswith("order:"):
            need(order_exists(ref[6:]), f"evidence points at unknown order: {ref}")
        elif ref.startswith("item:"):
            need(ref[5:] in known_item_refs, f"evidence points at unknown item: {ref}")
        elif ref.startswith("payment:"):
            need(ref[8:] in known_payment_refs, f"evidence points at unknown payment: {ref}")
        elif ref.startswith("seller:"):
            need(seller_exists(ref[7:]), f"evidence points at unknown seller: {ref}")
        elif ref.startswith("policy:"):
            need(ref[7:] in VALID_ROOT_CAUSES, f"evidence points at unknown policy code: {ref}")
        else:
            errors.append(f"evidence id has no valid prefix: {ref}")

    # -- financial + actions ----------------------------------------------
    f = doc["financial_resolution"]
    need(f.get("currency") == "BRL", "financial currency must be BRL")
    refund = f.get("recommended_refund_brl")
    need(_is_num(refund) and refund >= 0, f"bad recommended_refund_brl: {refund!r}")
    if _is_num(refund):
        need(round(refund, 2) == refund, "recommended_refund_brl must be rounded to 2 decimals")
        need(
            (refund > 0) == (a.get("case_status") == "action_required"),
            "case_status does not agree with the refund amount",
        )

    acts = doc["resolution_actions"]
    need(isinstance(acts, list) and 0 < len(acts) <= LIMITS["resolution_actions"],
         "resolution_actions must hold 1..5 entries")
    need(all(x in VALID_ACTIONS for x in acts), f"unknown action in {acts}")
    need(len(set(acts)) == len(acts), "duplicate resolution_actions")

    return errors
