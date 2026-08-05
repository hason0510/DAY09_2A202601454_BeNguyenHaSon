"""Coordinator: the per-case conversation, start to finish.

The coordinator owns no data scope of its own. It can only move envelopes
between specialists and merge what they hand back, which is what forces the
work to actually be distributed instead of collapsing into one prompt.

Conversation shape for one case:

    intake ─┐
            ├─ customer ─┐
            ├─ order+product ──(item sums)──> payment ─┐
            └─ delivery ───────────────────────────────┤
                                                        v
                                                    policy (rules + LLM 2nd opinion)
                                                        v
                                                    assemble draft
                                                        v
                                                     critic (adversarial)
                                                        v
                                                  verifier -> repair? -> verifier
                                                        v
                                                   output/EC_xxx.json
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .a2a.bus import Bus
from .a2a.message import Message
from .assembler import build_document, repair
from .config import MAX_REPAIR_ROUNDS
from .policy import rules

COORDINATOR = "coordinator"


@dataclass
class CaseOutcome:
    case_id: str
    document: dict | None
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    primary_issue: str | None = None
    adjudicator_agrees: bool | None = None
    critic_findings: int = 0
    repair_rounds: int = 0
    degraded: bool = False


class Coordinator:
    def __init__(self, bus: Bus, tracer, run_id: str) -> None:
        self._bus = bus
        self._tracer = tracer
        self._run_id = run_id

    def _ask(self, case_id: str, recipient: str, intent: str, payload: dict) -> dict:
        reply = self._bus.send(
            Message(
                run_id=self._run_id,
                case_id=case_id,
                sender=COORDINATOR,
                recipient=recipient,
                intent=intent,
                payload=payload,
            )
        )
        return reply.payload

    def run_case(self, case: dict) -> CaseOutcome:
        case_id = case["case_id"]
        order_id = case["customer_request"]["claimed_order_id"]
        notes: list[str] = []
        degraded = False

        self._tracer.emit(
            "case_start", case_id=case_id, order_id=order_id,
            policy_version=case.get("policy_version"),
        )

        # -- 1. read the complaint ----------------------------------------
        intake = self._ask(
            case_id, "intake_agent", "parse_claim",
            {
                "customer_request": case["customer_request"],
                "investigation_scope": case.get("investigation_scope", {}),
            },
        )
        degraded |= intake["degraded"]

        # -- 2. fan out to the fact agents --------------------------------
        customer = self._ask(case_id, "customer_agent", "resolve_identity", {"order_id": order_id})
        order_product = self._ask(
            case_id, "order_product_agent", "inspect_order", {"order_id": order_id}
        )
        delivery = self._ask(case_id, "delivery_agent", "analyse_delivery", {"order_id": order_id})

        of = order_product["facts"]
        # -- 3. real handoff: payment agent cannot see item rows, so the item
        #       sums have to travel to it inside the envelope.
        payment = self._ask(
            case_id, "payment_agent", "reconcile",
            {
                "order_id": order_id,
                "item_sum_raw": of.get("_item_sum_raw", 0.0),
                "freight_sum_raw": of.get("_freight_sum_raw", 0.0),
                "has_items": bool(of.get("item_row_count", 0)),
            },
        )

        for report in (customer, order_product, delivery, payment):
            degraded |= report["degraded"]
            notes.extend(report["notes"])

        facts = self._merge(order_id, customer, order_product, delivery, payment)

        # -- 4. policy: deterministic verdict + independent LLM opinion ----
        policy = self._ask(case_id, "policy_agent", "apply_policy", {"facts": facts})
        degraded |= policy["degraded"]
        if "verdict" not in policy["facts"]:
            self._tracer.emit("case_failed", case_id=case_id, reason="policy agent produced no verdict")
            return CaseOutcome(
                case_id=case_id, document=None,
                errors=["policy agent produced no verdict"],
                notes=notes + policy["notes"], degraded=True,
            )
        verdict = policy["facts"]["verdict"]
        agrees = policy["facts"].get("adjudicator_agrees")
        notes.extend(policy["notes"])

        # -- 5. assemble the draft ----------------------------------------
        confidence = rules.score_confidence(
            _as_verdict(verdict, facts), facts, adjudicator_agrees=agrees
        )
        draft = build_document(case_id, facts, verdict, confidence)

        # -- 6. adversarial pass, observer only ---------------------------
        # The critic reviews every draft and everything it says is written to
        # trace.jsonl, but it changes no field -- not even confidence. Its
        # measured false-positive rate never converged; see rules.score_confidence.
        critique = self._ask(
            case_id, "critic_agent", "challenge_draft", {"facts": facts, "draft": draft}
        )
        findings = critique["facts"].get("findings", []) if critique["facts"] else []
        degraded |= critique["degraded"]
        if findings:
            self._tracer.emit("critic_findings", case_id=case_id, findings=findings, advisory=True)
            notes.append(f"critic raised {len(findings)} advisory finding(s)")

        # -- 7. verify, repair once if needed, verify again ----------------
        rounds = 0
        verify = self._ask(case_id, "verifier_agent", "validate", {"draft": draft, "order_id": order_id})
        errors = verify["facts"].get("errors", [])
        while errors and rounds < MAX_REPAIR_ROUNDS:
            rounds += 1
            self._tracer.emit("repair", case_id=case_id, round=rounds, errors=errors[:5])
            draft = repair(
                draft, facts,
                known_items=set(facts["item_refs"]),
                known_payments=set(facts["payment_refs"]),
            )
            verify = self._ask(
                case_id, "verifier_agent", "validate", {"draft": draft, "order_id": order_id}
            )
            errors = verify["facts"].get("errors", [])

        self._tracer.emit(
            "case_end", case_id=case_id,
            primary_issue=verdict["primary_issue"],
            secondary_issues=verdict["secondary_issues"],
            refund_brl=verdict["recommended_refund_brl"],
            confidence=confidence,
            adjudicator_agrees=agrees,
            critic_findings=len(findings),  # logged, not priced in
            repair_rounds=rounds,
            schema_errors=errors,
        )

        return CaseOutcome(
            case_id=case_id, document=draft, errors=errors, notes=notes,
            primary_issue=verdict["primary_issue"], adjudicator_agrees=agrees,
            critic_findings=len(findings), repair_rounds=rounds, degraded=degraded,
        )

    @staticmethod
    def _merge(order_id, customer, order_product, delivery, payment) -> dict[str, Any]:
        """One flat fact sheet. Defaults keep a degraded agent from crashing the
        policy layer -- a missing block becomes an empty block, never a guess."""
        cf, of = customer["facts"], order_product["facts"]
        df, pf = delivery["facts"], payment["facts"]
        return {
            "order_id": order_id,
            "customer_unique_id": cf.get("customer_unique_id", ""),
            "related_order_ids": cf.get("related_order_ids", []),
            "related_order_count": cf.get("related_order_count", 0),
            "order_status": of.get("order_status", ""),
            "item_refs": of.get("item_refs", []),
            "item_row_count": of.get("item_row_count", 0),
            "seller_ids_all": of.get("seller_ids_all", []),
            "product_ids_all": of.get("product_ids_all", []),
            "category_names_all": of.get("category_names_all", []),
            "item_total_brl": of.get("item_total_brl", 0.0),
            "freight_total_brl": of.get("freight_total_brl", 0.0),
            "delivered_at": df.get("delivered_at"),
            "estimated_delivery_at": df.get("estimated_delivery_at"),
            "carrier_handoff_at": df.get("carrier_handoff_at"),
            "delivery_variance_hours": df.get("delivery_variance_hours"),
            "seller_handoff_analysis": df.get("seller_handoff_analysis", []),
            "late_handoff_seller_ids": df.get("late_handoff_seller_ids", []),
            "payment_refs": pf.get("payment_refs", []),
            "payment_row_count": pf.get("payment_row_count", 0),
            "payment_total_brl": pf.get("payment_total_brl", 0.0),
            "payment_types": pf.get("payment_types", []),
            "expected_total_brl": pf.get("expected_total_brl"),
            "difference_brl": pf.get("difference_brl"),
            "reconciled": pf.get("reconciled"),
        }


def _as_verdict(verdict: dict, facts: dict) -> rules.Verdict:
    """Rehydrate the dataclass the confidence function expects."""
    return rules.Verdict(
        primary_issue=verdict["primary_issue"],
        secondary_issues=verdict["secondary_issues"],
        root_cause_code=verdict["root_cause_code"],
        responsible_parties=verdict["responsible_parties"],
        recommended_refund_brl=verdict["recommended_refund_brl"],
        resolution_actions=verdict["resolution_actions"],
        case_status=verdict["case_status"],
        responsible_seller_ids=verdict["responsible_seller_ids"],
        used_fallback=verdict.get("used_fallback", False),
    )
