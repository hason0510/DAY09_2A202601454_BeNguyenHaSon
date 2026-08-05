"""Turn the merged fact sheet + policy verdict into the graded JSON document.

Two jobs, both boring on purpose:
  * place every value in the exact shape README section 6 asks for;
  * enforce the array caps and the no-item null rule at the point of writing,
    so the verifier is a second opinion rather than the only line of defence.
"""

from __future__ import annotations

from . import config
from .policy import schema


def _ranked_causes(facts: dict, verdict: dict) -> list[dict]:
    """Rank 1 is always the cause behind the primary issue.

    With RANKED_CAUSES == "contributing" we also rank the other codes the data
    independently supports. README's example shows a single cause, so "single"
    stays the default; this switch exists to A/B the other reading.
    """
    primary_code = verdict["root_cause_code"]
    codes = [primary_code]

    if config.RANKED_CAUSES == "contributing":
        variance = facts["delivery_variance_hours"]
        arrived_late = (
            facts["delivered_at"] is not None
            and facts["estimated_delivery_at"] is not None
            and (variance or 0.0) > 0
        )
        # A seller-caused delay still ends with the carrier delivering late.
        if primary_code == "SELLER_HANDOFF_AFTER_LIMIT" and arrived_late:
            codes.append("CARRIER_DELIVERED_AFTER_ESTIMATE")
        # A reconciled split payment on a punctual order supports both codes.
        if primary_code == "MULTIPLE_PAYMENTS_RECONCILED" and not arrived_late:
            codes.append("DELIVERY_WITHIN_ESTIMATE")
        if primary_code == "DELIVERY_WITHIN_ESTIMATE" and facts["payment_row_count"] >= 2:
            codes.append("MULTIPLE_PAYMENTS_RECONCILED")

    codes = codes[: schema.LIMITS["ranked_causes"]]
    return [{"cause_code": code, "rank": i} for i, code in enumerate(codes, start=1)]


def _evidence(facts: dict, verdict: dict) -> list[str]:
    """order -> items -> payments -> responsible sellers -> policy, capped at 20.

    The order line and the policy line are reserved first: they identify the
    case and the rule applied, so they must survive any truncation.
    """
    order_id = facts["order_id"]
    reserved = [f"order:{order_id}"]
    seller_ids = (
        facts["seller_ids_all"]
        if config.EVIDENCE_SELLERS == "all"
        else verdict["responsible_seller_ids"]
    )
    sellers = [f"seller:{sid}" for sid in seller_ids]
    policy = [f"policy:{verdict['root_cause_code']}"]

    budget = schema.LIMITS["evidence_ids"] - len(reserved) - len(sellers) - len(policy)
    fillers: list[str] = []
    for ref in facts["item_refs"]:
        fillers.append(f"item:{ref}")
    for ref in facts["payment_refs"]:
        fillers.append(f"payment:{ref}")

    out = reserved + fillers[: max(budget, 0)] + sellers + policy
    # Preserve order while dropping any accidental duplicate.
    seen: set[str] = set()
    deduped = []
    for ref in out:
        if ref not in seen:
            seen.add(ref)
            deduped.append(ref)
    return deduped[: schema.LIMITS["evidence_ids"]]


def normalize_zeros(node):
    """Collapse every -0.0 in the finished document to 0.0.

    A belt-and-braces sweep: the rounding helpers already normalise, but a value
    can reach the document from anywhere, and "-0.0" is a different token from
    "0.0" to a strict grader.
    """
    if isinstance(node, dict):
        return {k: normalize_zeros(v) for k, v in node.items()}
    if isinstance(node, list):
        return [normalize_zeros(v) for v in node]
    if isinstance(node, float) and node == 0.0:
        return 0.0
    return node


def build_document(case_id: str, facts: dict, verdict: dict, confidence: float) -> dict:
    has_items = facts["item_row_count"] > 0
    order_id = facts["order_id"]

    return normalize_zeros({
        "case_id": case_id,
        "case_assessment": {
            "primary_issue": verdict["primary_issue"],
            "secondary_issues": list(verdict["secondary_issues"]),
            "case_status": verdict["case_status"],
            "confidence": confidence,
        },
        "affected_entities": {
            # Only the claimed order. History lives in customer_context.
            "order_ids": schema.cap([order_id], "order_ids"),
            "item_ids": schema.cap(facts["item_refs"], "item_ids"),
            "seller_ids": schema.cap(facts["seller_ids_all"], "seller_ids"),
            "payment_ids": schema.cap(facts["payment_refs"], "payment_ids"),
        },
        "customer_context": {
            "customer_unique_id": facts["customer_unique_id"],
            "related_order_ids": schema.cap(facts["related_order_ids"], "related_order_ids"),
        },
        "product_context": {
            "product_ids": schema.cap(facts["product_ids_all"], "product_ids"),
            "category_names": schema.cap(facts["category_names_all"], "category_names"),
        },
        "delivery_analysis": {
            "delivered_at": facts["delivered_at"],
            "estimated_delivery_at": facts["estimated_delivery_at"],
            "carrier_handoff_at": facts["carrier_handoff_at"],
            "delivery_variance_hours": facts["delivery_variance_hours"],
            "seller_handoff_analysis": facts["seller_handoff_analysis"],
            "late_handoff_seller_ids": list(facts["late_handoff_seller_ids"]),
        },
        "payment_reconciliation": {
            "currency": "BRL",
            "item_total_brl": facts["item_total_brl"],
            "freight_total_brl": facts["freight_total_brl"],
            # README section 4 nulls these three -- and only these three --
            # when the order carries no item rows.
            "expected_total_brl": facts["expected_total_brl"] if has_items else None,
            "payment_total_brl": facts["payment_total_brl"],
            "difference_brl": facts["difference_brl"] if has_items else None,
            "reconciled": facts["reconciled"] if has_items else None,
            "payment_types": facts["payment_types"],
        },
        "root_cause_analysis": {
            "ranked_causes": _ranked_causes(facts, verdict),
            "responsible_parties": schema.cap(
                verdict["responsible_parties"], "responsible_parties"
            ),
        },
        "evidence_ids": _evidence(facts, verdict),
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": verdict["recommended_refund_brl"],
        },
        "resolution_actions": schema.cap(verdict["resolution_actions"], "resolution_actions"),
    })


def repair(doc: dict, facts: dict, known_items: set[str], known_payments: set[str]) -> dict:
    """Deterministic normalisation applied when the verifier rejects a draft.

    Every fix here is a truncation, a de-duplication or a null coercion -- never
    a re-decision. If a draft is wrong about the verdict itself, repair will not
    hide it; the case stays flagged in the run report.
    """
    doc = dict(doc)

    a = dict(doc["case_assessment"])
    conf = a.get("confidence")
    a["confidence"] = 0.5 if not isinstance(conf, (int, float)) else round(
        max(0.0, min(1.0, float(conf))), 2
    )
    doc["case_assessment"] = a

    e = dict(doc["affected_entities"])
    e["order_ids"] = schema.cap(e.get("order_ids", []), "order_ids")
    e["item_ids"] = schema.cap(
        [x for x in e.get("item_ids", []) if x in known_items], "item_ids"
    )
    e["payment_ids"] = schema.cap(
        [x for x in e.get("payment_ids", []) if x in known_payments], "payment_ids"
    )
    e["seller_ids"] = schema.cap(e.get("seller_ids", []), "seller_ids")
    doc["affected_entities"] = e

    c = dict(doc["customer_context"])
    c["related_order_ids"] = schema.cap(
        [x for x in c.get("related_order_ids", []) if x not in e["order_ids"]],
        "related_order_ids",
    )
    doc["customer_context"] = c

    p = dict(doc["product_context"])
    p["product_ids"] = schema.cap(p.get("product_ids", []), "product_ids")
    p["category_names"] = schema.cap(p.get("category_names", []), "category_names")

    r = dict(doc["payment_reconciliation"])
    if not e["item_ids"]:
        r["expected_total_brl"] = None
        r["difference_brl"] = None
        r["reconciled"] = None
        p["product_ids"] = []
        p["category_names"] = []
        e["seller_ids"] = []
        d = dict(doc["delivery_analysis"])
        d["seller_handoff_analysis"] = []
        d["late_handoff_seller_ids"] = []
        doc["delivery_analysis"] = d
    doc["payment_reconciliation"] = r
    doc["product_context"] = p

    valid_refs = (
        {f"order:{x}" for x in e["order_ids"]}
        | {f"item:{x}" for x in known_items}
        | {f"payment:{x}" for x in known_payments}
    )
    kept: list[str] = []
    for ref in doc.get("evidence_ids", []):
        ok = ref.startswith(("seller:", "policy:")) or ref in valid_refs
        if ok and ref not in kept:
            kept.append(ref)
    doc["evidence_ids"] = kept[: schema.LIMITS["evidence_ids"]]

    actions: list[str] = []
    for act in doc.get("resolution_actions", []):
        if act in schema.VALID_ACTIONS and act not in actions:
            actions.append(act)
    doc["resolution_actions"] = actions[: schema.LIMITS["resolution_actions"]]

    return doc
