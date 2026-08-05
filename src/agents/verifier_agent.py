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
    role = "Gate the incoming request, then validate schema, caps, nulls and evidence IDs"
    capabilities = frozenset({"orders", "items", "payments", "sellers", "customers"})

    def handle(self, message: Message, view: DataView) -> AgentReport:
        # Two intents: gate the request on the way in, gate the document on the
        # way out. Same agent because both are "check it against the CSVs".
        if message.intent == "validate_request":
            problems = schema.validate_request(
                message.payload["case"],
                expected_case_id=message.case_id,
                expected_policy_version=message.payload["policy_version"],
                order_exists=lambda oid: view.order(oid) is not None,
            )
            return AgentReport(
                facts={"errors": problems, "passed": not problems},
                notes=(["request accepted"] if not problems else problems[:5]),
                degraded=bool(problems),
            )

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

        anomalies: list[str] = []
        if not errors:
            # Business gates run on a structurally sound document only -- on a
            # malformed one they would report noise about missing keys.
            order = view.order(order_id)
            customer = view.customer(order.customer_id) if order else None
            business, anomalies = schema.validate_business(
                doc,
                order_id=order_id,
                order_status=order.order_status if order else "",
                order_sellers={i.seller_id for i in view.items(order_id)},
                customer_unique_id=customer.customer_unique_id if customer else "",
            )
            errors = business

        notes = ["schema and business rules clean"] if not errors else (
            [f"{len(errors)} violation(s)"] + errors[:5]
        )
        notes += [f"data anomaly: {a}" for a in anomalies]

        return AgentReport(
            facts={"errors": errors, "anomalies": anomalies, "passed": not errors},
            notes=notes,
            degraded=bool(errors),
        )
