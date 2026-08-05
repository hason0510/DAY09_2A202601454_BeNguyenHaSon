"""Deterministic gatekeeper: nothing reaches output/ without passing here.

The verifier re-derives the ID universe from the CSVs itself instead of
trusting the draft, so a fabricated item or seller reference cannot slip past by
being internally consistent.
"""

from __future__ import annotations

from ..a2a.message import AgentReport, Message
from ..policy import schema
from ..store.views import DataView
from .base import Agent


class VerifierAgent(Agent):
    name = "verifier_agent"
    role = "Validate schema, array caps, null handling and evidence IDs against the CSVs"
    capabilities = frozenset({"orders", "items", "payments", "sellers"})

    def handle(self, message: Message, view: DataView) -> AgentReport:
        doc = message.payload["draft"]
        case_id = message.case_id
        order_id = message.payload["order_id"]

        # Independently rebuilt from source, not from the draft.
        known_items = {i.item_ref for i in view.items(order_id)}
        known_payments = {p.payment_ref for p in view.payments(order_id)}

        errors = schema.validate(
            doc,
            expected_case_id=case_id,
            order_exists=lambda oid: view.order(oid) is not None,
            seller_exists=view.seller_exists,
            known_item_refs=known_items,
            known_payment_refs=known_payments,
        )

        return AgentReport(
            facts={"errors": errors, "passed": not errors},
            notes=(["schema clean"] if not errors else [f"{len(errors)} violation(s)"] + errors[:5]),
            degraded=bool(errors),
        )
