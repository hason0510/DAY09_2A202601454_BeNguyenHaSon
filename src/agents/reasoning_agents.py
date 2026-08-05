"""The three LLM agents.

Deliberate boundary: these agents may influence exactly one graded field --
``confidence``, the only field README leaves undefined -- and nothing else.
Every objective value is produced by policy/rules.py. So a hallucination costs
us a rounding error in one sub-field instead of a wrong refund amount, while the
agents still do real work: reading the Vietnamese complaint, forming an
independent verdict, and attacking the draft.
"""

from __future__ import annotations

import json

from ..a2a.message import AgentReport, Message
from ..policy import rules
from ..store.views import DataView
from .base import Agent

INTAKE_SYSTEM = """You triage e-commerce complaints for a Brazilian marketplace.
Read the customer's message (it may be Vietnamese) and the investigation scope.
Return JSON only:
{"claimed_problem": one of ["late_delivery","order_never_arrived","payment_mismatch","refund_request","general_investigation"],
 "asks_for_refund": true|false,
 "history_requested": true|false,
 "product_context_requested": true|false,
 "summary_en": "<= 20 words"}
Report only what the customer actually says. Never invent order facts."""

ADJUDICATOR_SYSTEM = """You are a dispute adjudicator applying policy EC_POLICY_V2.
The fact sheet is already verified and already contains every comparison you
need as an explicit boolean. Never re-derive a comparison from a timestamp or a
signed number: trust the boolean.

Walk the rules top to bottom and STOP at the first one whose condition holds:
1 canceled_order_paid      - order_status == "canceled"    AND payment_total_brl > 0
2 unavailable_order_paid   - order_status == "unavailable" AND payment_total_brl > 0
3 late_delivery_seller     - delivered_after_estimate == true AND late_handoff_seller_count >= 1
4 late_delivery_logistics  - delivered_after_estimate == true AND late_handoff_seller_count == 0
5 valid_split_payment      - has_multiple_payment_rows == true AND reconciled == true
6 unsupported_late_claim   - delivered_after_estimate == false AND reconciled == true

delivered_after_estimate == true means the parcel arrived LATE; that is a real
breach even when the order eventually reached the customer, so rules 3 and 4
outrank rules 5 and 6. Only reach rule 6 when delivered_after_estimate is false.

Rules 1 and 2 do not look at reconciled or at delivery at all. An unavailable or
canceled order that was paid for is settled by rule 1 or 2 no matter what
reconciled says. reconciled == null only means the order has no item rows, so
there was nothing to reconcile -- it is NOT evidence that the claim is fine, and
it never sends you to rule 6.

Rules 5 and 6 both describe an order that arrived on time with the money adding
up, so they overlap. Rule 5 is listed first and therefore WINS: whenever
has_multiple_payment_rows == true and reconciled == true, answer
valid_split_payment. Reach unsupported_late_claim only when the order has a
single payment row. "The delivery was fine" is not a reason to skip rule 5 --
rule 5 is about how the customer paid, not about when the parcel arrived.

Return JSON only:
{"primary_issue": "<one of the six>", "reason": "<= 25 words"}"""

CRITIC_SYSTEM = """You are an adversarial reviewer of an e-commerce dispute resolution.
You get a verified fact sheet and a draft. Report ONLY a direct contradiction
between the draft and a named key in the fact sheet.

Refund AMOUNTS are out of scope: they are verified arithmetically elsewhere and
you are not shown them. Judge only the categorical fields.

EC_POLICY_V2 pairs each verdict with a case_status. These are CORRECT BY
DEFINITION -- never object to one of them:

  canceled_order_paid     -> action_required   (party: platform)
  unavailable_order_paid  -> action_required   (party: platform)
  late_delivery_seller    -> action_required   (party: seller)
  late_delivery_logistics -> action_required   (party: logistics_provider)
  valid_split_payment     -> no_action         (no responsible party)
  unsupported_late_claim  -> no_action         (no responsible party)

A canceled or unavailable paid order is action_required even though it was never
delivered. A late parcel is action_required even though it did arrive.

Read these keys the way the policy defines them:
- delivered_after_estimate == true means the parcel arrived LATE; false means it
  arrived on time, and an on-time parcel must NOT be described as late.
- reconciled == true only means the money adds up. It neither implies an on-time
  delivery nor rules out a refund.
- reconciled == null means the order has no item rows, so there was nothing to
  reconcile. This is NOT a data error.

You are checking the draft against the fact sheet, NOT re-designing the policy.
Do not object to a refund amount that matches the table above.

First state your overall assessment, then list findings only if you chose
"contradicted". Most drafts are consistent -- answering "consistent" with an
empty list is the normal, successful outcome of this review. Never put an
agreement or a confirmation inside findings; findings are for defects only.

Return JSON only:
{"assessment": "consistent" | "contradicted",
 "findings": [{"field": "<output field>", "fact_key": "<fact sheet key>", "problem": "<= 20 words"}]}
At most 3 findings."""


class IntakeAgent(Agent):
    """Turns the free-text complaint into a structured claim."""

    name = "intake_agent"
    role = "Parse the customer complaint into a structured claim (LLM)"
    capabilities = frozenset()  # reads the request only, never the database
    uses_llm = True

    def __init__(self, llm) -> None:
        self._llm = llm

    def handle(self, message: Message, view: DataView) -> AgentReport:
        request = message.payload["customer_request"]
        scope = message.payload.get("investigation_scope", {})
        parsed = self._llm.ask_json(
            system=INTAKE_SYSTEM,
            user=json.dumps(
                {"customer_request": request, "investigation_scope": scope},
                ensure_ascii=False,
            ),
            purpose="intake",
            case_id=message.case_id,
        )
        if not parsed:
            return AgentReport(
                facts={"claimed_problem": None, "asks_for_refund": None},
                notes=["intake LLM unavailable; scope flags taken from the input file"],
                degraded=True,
            )
        return AgentReport(
            facts={
                "claimed_problem": parsed.get("claimed_problem"),
                "asks_for_refund": parsed.get("asks_for_refund"),
                "summary_en": parsed.get("summary_en", ""),
            },
            notes=[f"claim parsed as {parsed.get('claimed_problem')!r}"],
        )


class PolicyAgent(Agent):
    """Deterministic EC_POLICY_V2 engine + an independent LLM second opinion."""

    name = "policy_agent"
    role = "Apply EC_POLICY_V2 (rule engine, authoritative) and cross-check with an LLM adjudicator"
    capabilities = frozenset()  # decides from handed-over facts only
    uses_llm = True

    def __init__(self, llm) -> None:
        self._llm = llm

    def handle(self, message: Message, view: DataView) -> AgentReport:
        facts = message.payload["facts"]
        verdict = rules.apply_policy(facts)

        second = self._llm.ask_json(
            system=ADJUDICATOR_SYSTEM,
            user=json.dumps(self._fact_sheet(facts), ensure_ascii=False),
            purpose="adjudicate",
            case_id=message.case_id,
        )
        agrees: bool | None = None
        llm_choice = None
        if second:
            llm_choice = second.get("primary_issue")
            if llm_choice in rules.ROOT_CAUSE:
                agrees = llm_choice == verdict.primary_issue

        return AgentReport(
            facts={
                "verdict": {
                    "primary_issue": verdict.primary_issue,
                    "secondary_issues": verdict.secondary_issues,
                    "root_cause_code": verdict.root_cause_code,
                    "responsible_parties": verdict.responsible_parties,
                    "responsible_seller_ids": verdict.responsible_seller_ids,
                    "recommended_refund_brl": verdict.recommended_refund_brl,
                    "resolution_actions": verdict.resolution_actions,
                    "case_status": verdict.case_status,
                    "rationale": verdict.rationale,
                    "used_fallback": verdict.used_fallback,
                },
                "adjudicator_choice": llm_choice,
                "adjudicator_agrees": agrees,
            },
            evidence=[f"policy:{verdict.root_cause_code}"],
            notes=[
                f"rule engine -> {verdict.primary_issue}; "
                f"llm adjudicator -> {llm_choice}; agrees={agrees}"
            ],
        )

    @staticmethod
    def _fact_sheet(facts: dict) -> dict:
        """The minimal, pre-computed view the LLM agents are allowed to see.

        Every comparison the policy depends on is resolved here into a boolean.
        An earlier revision passed raw timestamps and a signed variance and let
        the model infer "is this late?"; it silently read every positive
        variance as on-time and disagreed with 18/50 verdicts. Comparisons are
        the deterministic layer's job -- the model only applies precedence.
        """
        variance = facts["delivery_variance_hours"]
        delivered_after_estimate = (
            facts["delivered_at"] is not None
            and facts["estimated_delivery_at"] is not None
            and (variance or 0.0) > 0
        )
        return {
            "order_status": facts["order_status"],
            "payment_total_brl": facts["payment_total_brl"],
            # The two refund bases. Without these the critic cannot check that
            # a freight refund really equals the freight total, and defaults to
            # objecting on every late-delivery case.
            "freight_total_brl": facts["freight_total_brl"],
            "item_total_brl": facts["item_total_brl"],
            "payment_row_count": facts["payment_row_count"],
            "has_multiple_payment_rows": facts["payment_row_count"] >= 2,
            "reconciled": facts["reconciled"],
            "difference_brl": facts["difference_brl"],
            "delivered_at": facts["delivered_at"],
            "estimated_delivery_at": facts["estimated_delivery_at"],
            "delivery_variance_hours": variance,
            "delivered_after_estimate": delivered_after_estimate,
            "hours_late": variance if delivered_after_estimate else 0.0,
            "late_handoff_seller_count": len(facts["late_handoff_seller_ids"]),
            "item_row_count": facts["item_row_count"],
            "has_item_rows": facts["item_row_count"] > 0,
        }


# The draft fields the critic is shown, and therefore the only ones it may name.
#
# recommended_refund_brl is deliberately absent. Measured on this dataset,
# gpt-4o-mini could not reliably compare the drafted amount against the amount
# in the fact sheet: it produced "refund should be full payment total, not
# 85.14" on cases where 85.14 was exactly the payment total, on 21/50 cases.
# Amount checking belongs to the verifier, which compares floats in Python.
# The critic keeps the categorical work a language model is actually good at.
CRITIQUABLE_FIELDS = frozenset(
    {
        "primary_issue",
        "secondary_issues",
        "case_status",
        "responsible_parties",
        "resolution_actions",
    }
)


class CriticAgent(Agent):
    """Adversarial pass over the assembled draft. Advisory: it can lower
    confidence and it always lands in the trace, but it cannot rewrite a field."""

    name = "critic_agent"
    role = "Attack the draft resolution for contradictions against the facts (LLM)"
    capabilities = frozenset()
    uses_llm = True

    def __init__(self, llm) -> None:
        self._llm = llm

    def handle(self, message: Message, view: DataView) -> AgentReport:
        fact_sheet = PolicyAgent._fact_sheet(message.payload["facts"])
        payload = {
            "fact_sheet": fact_sheet,
            "draft": {
                "primary_issue": message.payload["draft"]["case_assessment"]["primary_issue"],
                "secondary_issues": message.payload["draft"]["case_assessment"]["secondary_issues"],
                "case_status": message.payload["draft"]["case_assessment"]["case_status"],
                "responsible_parties": message.payload["draft"]["root_cause_analysis"][
                    "responsible_parties"
                ],
                # recommended_refund_brl withheld on purpose -- see CRITIQUABLE_FIELDS.
                "resolution_actions": message.payload["draft"]["resolution_actions"],
            },
        }
        result = self._llm.ask_json(
            system=CRITIC_SYSTEM,
            user=json.dumps(payload, ensure_ascii=False),
            purpose="critique",
            case_id=message.case_id,
        )
        if not result:
            return AgentReport(
                facts={"findings": []},
                notes=["critic LLM unavailable"],
                degraded=True,
            )
        # An explicit agreement channel. Without one, the model fills the
        # findings array with confirmations ("full refund is correct, no
        # contradiction") and every clean case looks disputed.
        if result.get("assessment") == "consistent":
            return AgentReport(facts={"findings": [], "discarded": []}, notes=["draft consistent"])

        raw = result.get("findings") or []
        if not isinstance(raw, list):
            raw = []

        # A finding only counts if it points at a key that actually exists in
        # the fact sheet. An objection the critic cannot ground in a fact is an
        # opinion, and opinions must not move a graded field.
        findings, discarded = [], []
        for item in raw[:3]:
            grounded = (
                isinstance(item, dict)
                and item.get("fact_key") in fact_sheet
                and item.get("field") in CRITIQUABLE_FIELDS
            )
            (findings if grounded else discarded).append(item)

        notes = [f"{len(findings)} grounded contradiction(s) raised"]
        if discarded:
            notes.append(f"{len(discarded)} ungrounded objection(s) discarded")

        return AgentReport(facts={"findings": findings, "discarded": discarded}, notes=notes)
