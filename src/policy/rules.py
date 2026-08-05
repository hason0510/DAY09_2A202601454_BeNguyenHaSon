"""EC_POLICY_V2 as a deterministic decision function.

This module is the authority for every graded value. The LLM agents may argue
with it, and their disagreement is recorded and priced into ``confidence``, but
they can never overwrite it -- 85% of the rubric is arithmetic on CSV rows, and
an 8B-class model is not the right instrument for arithmetic.

Rule precedence follows README section 4 top to bottom.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Primary issue -> (root cause code, primary action)
ROOT_CAUSE = {
    "canceled_order_paid": "ORDER_CANCELED_AFTER_PAYMENT",
    "unavailable_order_paid": "ORDER_UNAVAILABLE_AFTER_PAYMENT",
    "late_delivery_seller": "SELLER_HANDOFF_AFTER_LIMIT",
    "late_delivery_logistics": "CARRIER_DELIVERED_AFTER_ESTIMATE",
    "valid_split_payment": "MULTIPLE_PAYMENTS_RECONCILED",
    "unsupported_late_claim": "DELIVERY_WITHIN_ESTIMATE",
}

PRIMARY_ACTION = {
    "canceled_order_paid": "issue_full_refund",
    "unavailable_order_paid": "issue_full_refund",
    "late_delivery_seller": "refund_freight",
    "late_delivery_logistics": "refund_freight",
    "valid_split_payment": "explain_valid_split_payment",
    "unsupported_late_claim": "reject_late_refund",
}

# Base confidence per verdict: how much the CSV alone pins the answer down.
# canceled/unavailable are pure status lookups; the late-delivery split depends
# on timestamps that can be missing, so it starts lower.
BASE_CONFIDENCE = {
    "canceled_order_paid": 0.95,
    "unavailable_order_paid": 0.95,
    "late_delivery_seller": 0.93,
    "late_delivery_logistics": 0.91,
    "valid_split_payment": 0.89,
    "unsupported_late_claim": 0.87,
}

RECONCILE_TOLERANCE_BRL = 0.10

SECONDARY_ORDER = [
    "multi_item_order",
    "multi_seller_order",
    "split_payment",
    "repeat_customer",
    "multiple_categories",
]


@dataclass
class Verdict:
    primary_issue: str
    secondary_issues: list[str]
    root_cause_code: str
    responsible_parties: list[dict[str, str]]
    recommended_refund_brl: float
    resolution_actions: list[str]
    case_status: str
    responsible_seller_ids: list[str] = field(default_factory=list)
    rationale: list[str] = field(default_factory=list)
    used_fallback: bool = False


def _r2(value: float) -> float:
    return round(value + 0.0, 2)


def decide_primary(facts: dict) -> tuple[str, list[str], bool]:
    """Return (primary_issue, rationale, used_fallback).

    ``facts`` is the merged fact sheet produced by the worker agents.
    """
    status = facts["order_status"]
    payment_total = facts["payment_total_brl"] or 0.0
    delivered_at = facts["delivered_at"]
    estimated_at = facts["estimated_delivery_at"]
    variance = facts["delivery_variance_hours"]
    late_sellers = facts["late_handoff_seller_ids"]
    payment_rows = facts["payment_row_count"]
    reconciled = facts["reconciled"]
    why: list[str] = []

    # Rule 1 / 2 -- money taken for an order that will never arrive.
    if status == "canceled" and payment_total > 0:
        why.append(f"order_status=canceled and payment_total={payment_total} > 0")
        return "canceled_order_paid", why, False
    if status == "unavailable" and payment_total > 0:
        why.append(f"order_status=unavailable and payment_total={payment_total} > 0")
        return "unavailable_order_paid", why, False

    # Rule 3 / 4 -- late delivery, split by who caused it.
    delivered_late = (
        delivered_at is not None and estimated_at is not None and (variance or 0.0) > 0
    )
    if delivered_late:
        why.append(f"delivered {variance}h after the estimated date")
        if late_sellers:
            why.append(f"{len(late_sellers)} seller(s) handed off after shipping_limit_date")
            return "late_delivery_seller", why, False
        why.append("no seller handed off late -> carrier owns the delay")
        return "late_delivery_logistics", why, False

    # Rule 5 -- multi-row payment that actually adds up.
    if payment_rows >= 2 and reconciled is True:
        why.append(f"{payment_rows} payment rows reconcile within {RECONCILE_TOLERANCE_BRL} BRL")
        return "valid_split_payment", why, False

    # Rule 6 -- delivered on time and the money matches: nothing to refund.
    if delivered_at is not None and estimated_at is not None and (variance or 0.0) <= 0:
        if reconciled is True:
            why.append("delivered on or before the estimated date and payment reconciles")
            return "unsupported_late_claim", why, False
        why.append(
            "delivered on time but payment does not reconcile; "
            "EC_POLICY_V2 has no refund rule for a reconciliation gap alone"
        )
        return "unsupported_late_claim", why, True

    # No rule fires (e.g. canceled with zero payment). Closest safe verdict is
    # "no refund owed", flagged so the run report can surface it.
    why.append(
        f"no EC_POLICY_V2 rule matched (status={status}, payments={payment_rows}, "
        f"delivered={delivered_at is not None}); defaulting to no-refund"
    )
    return "unsupported_late_claim", why, True


def decide_secondary(facts: dict) -> list[str]:
    """Secondary issues, appended strictly in README order."""
    found: list[str] = []
    if facts["item_row_count"] >= 2:
        found.append("multi_item_order")
    if len(facts["seller_ids_all"]) >= 2:
        found.append("multi_seller_order")
    if facts["payment_row_count"] >= 2:
        found.append("split_payment")
    if facts["related_order_count"] >= 1:
        found.append("repeat_customer")
    if len(facts["category_names_all"]) >= 2:
        found.append("multiple_categories")
    return [s for s in SECONDARY_ORDER if s in found]


def _responsible(primary: str, facts: dict) -> tuple[list[dict[str, str]], list[str]]:
    if primary in ("canceled_order_paid", "unavailable_order_paid"):
        return [{"party_type": "platform", "party_id": "OLIST_PLATFORM"}], []
    if primary == "late_delivery_seller":
        sellers = facts["late_handoff_seller_ids"]
        return (
            [{"party_type": "seller", "party_id": sid} for sid in sellers],
            list(sellers),
        )
    if primary == "late_delivery_logistics":
        return (
            [{"party_type": "logistics_provider", "party_id": "LOGISTICS_PROVIDER"}],
            [],
        )
    return [], []  # valid_split_payment / unsupported_late_claim: nobody at fault


def _refund(primary: str, facts: dict) -> float:
    if primary in ("canceled_order_paid", "unavailable_order_paid"):
        return _r2(facts["payment_total_brl"] or 0.0)
    if primary in ("late_delivery_seller", "late_delivery_logistics"):
        return _r2(facts["freight_total_brl"] or 0.0)
    return 0.0


def _actions(primary: str, secondary: list[str], refund: float) -> list[str]:
    """Primary action first, then the supplementary slots in README order."""
    actions = [PRIMARY_ACTION[primary]]

    if primary == "late_delivery_seller":
        actions.append("review_seller_handoff")
    elif primary == "late_delivery_logistics":
        actions.append("review_carrier_delay")

    if refund > 0:
        actions.append("verify_refund_completion")
    if "multi_seller_order" in secondary:
        actions.append("coordinate_multi_seller_case")
    # Skipped for valid_split_payment: explain_valid_split_payment already
    # covers the split (README section 4).
    if "split_payment" in secondary and primary != "valid_split_payment":
        actions.append("verify_payment_allocation")

    return actions


def apply_policy(facts: dict) -> Verdict:
    primary, why, used_fallback = decide_primary(facts)
    secondary = decide_secondary(facts)
    parties, seller_ids = _responsible(primary, facts)
    refund = _refund(primary, facts)
    actions = _actions(primary, secondary, refund)

    return Verdict(
        primary_issue=primary,
        secondary_issues=secondary,
        root_cause_code=ROOT_CAUSE[primary],
        responsible_parties=parties,
        recommended_refund_brl=refund,
        resolution_actions=actions,
        case_status="action_required" if refund > 0 else "no_action",
        responsible_seller_ids=seller_ids,
        rationale=why,
        used_fallback=used_fallback,
    )


def score_confidence(
    verdict: Verdict,
    facts: dict,
    *,
    adjudicator_agrees: bool | None,
) -> float:
    """Confidence = how well the CSV pins this verdict down, minus doubt.

    EC_POLICY_V2 does not define this field, so we make it a transparent
    function of evidence quality and inter-agent agreement rather than an
    LLM-invented number.

    The critic's findings are deliberately NOT an input. Across five prompt
    revisions its false-positive rate on this dataset went 39 -> 29 -> 11 -> 21
    -> 36 cases out of 50 without converging, and the surviving objections
    restated the draft as if it were a contradiction ("draft states late
    delivery, but order was delivered late"). A signal that noisy must not move
    a graded field; the critic stays in the loop as a logged observer instead.
    See docs/design_notes.md, "What we measured about gpt-4o-mini".
    """
    score = BASE_CONFIDENCE[verdict.primary_issue]

    if adjudicator_agrees is True:
        score += 0.02
    elif adjudicator_agrees is False:
        score -= 0.08
    else:
        score -= 0.03  # adjudicator unavailable: no second opinion at all

    if verdict.used_fallback:
        score -= 0.10
    if facts["reconciled"] is False:
        score -= 0.05
    elif facts["reconciled"] is None:
        score -= 0.03  # no item rows: nothing to reconcile against
    if (
        verdict.primary_issue in ("late_delivery_seller", "late_delivery_logistics")
        and facts["carrier_handoff_at"] is None
    ):
        score -= 0.03

    return round(max(0.5, min(0.99, score)), 2)
