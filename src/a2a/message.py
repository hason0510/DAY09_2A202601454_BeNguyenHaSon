"""A2A message envelope.

Agents never call each other directly and never share mutable state. Every
handoff is an immutable envelope routed by the bus, which is what makes the run
traceable: trace.jsonl is literally the sequence of envelopes.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any

_counter = itertools.count(1)


def _next_id() -> str:
    return f"msg_{next(_counter):06d}"


@dataclass(frozen=True)
class Message:
    run_id: str
    case_id: str
    sender: str
    recipient: str
    intent: str
    payload: dict[str, Any] = field(default_factory=dict)
    msg_id: str = field(default_factory=_next_id)
    # Which message caused this one -- lets a reader reconstruct the call tree.
    causation_id: str | None = None

    def reply(self, payload: dict[str, Any], intent: str | None = None) -> "Message":
        return Message(
            run_id=self.run_id,
            case_id=self.case_id,
            sender=self.recipient,
            recipient=self.sender,
            intent=intent or f"{self.intent}.result",
            payload=payload,
            causation_id=self.msg_id,
        )


@dataclass
class AgentReport:
    """What a worker agent hands back: facts + the evidence backing them."""

    facts: dict[str, Any] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    degraded: bool = False

    def as_payload(self) -> dict[str, Any]:
        return {
            "facts": self.facts,
            "evidence": self.evidence,
            "notes": self.notes,
            "degraded": self.degraded,
        }
