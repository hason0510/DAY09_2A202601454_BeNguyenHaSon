"""The message bus: routing + capability enforcement + tracing in one place.

Because every handoff goes through ``send``, three properties hold for free:
  1. no agent can touch a table it did not declare (view is built per hop);
  2. every handoff appears in trace.jsonl with sender, recipient and intent;
  3. an agent crash degrades one case instead of killing the run.
"""

from __future__ import annotations

import traceback

from ..store.olist_store import OlistStore
from ..store.views import build_view
from .message import AgentReport, Message
from .trace import TraceWriter, summarize


class Bus:
    def __init__(self, store: OlistStore, tracer: TraceWriter) -> None:
        self._store = store
        self._tracer = tracer
        self._agents: dict[str, object] = {}

    def register(self, agent) -> None:
        self._agents[agent.name] = agent

    @property
    def roster(self) -> dict[str, dict]:
        return {
            name: {
                "role": getattr(a, "role", ""),
                "scopes": sorted(getattr(a, "capabilities", frozenset())),
                "uses_llm": bool(getattr(a, "uses_llm", False)),
            }
            for name, a in self._agents.items()
        }

    def send(self, message: Message) -> Message:
        agent = self._agents.get(message.recipient)
        if agent is None:
            raise KeyError(f"no agent registered as '{message.recipient}'")

        self._tracer.emit(
            "handoff",
            case_id=message.case_id,
            msg_id=message.msg_id,
            causation_id=message.causation_id,
            **{"from": message.sender},
            to=message.recipient,
            intent=message.intent,
            payload=summarize(message.payload),
        )

        view = build_view(self._store, agent.name, agent.capabilities)
        try:
            report = agent.handle(message, view)
        except Exception as exc:  # one bad case must not sink the batch
            self._tracer.emit(
                "agent_error",
                case_id=message.case_id,
                agent=agent.name,
                error=f"{type(exc).__name__}: {exc}",
                stack=traceback.format_exc(limit=4),
            )
            report = AgentReport(
                notes=[f"{agent.name} failed: {type(exc).__name__}: {exc}"],
                degraded=True,
            )

        reply = message.reply(report.as_payload())
        self._tracer.emit(
            "report",
            case_id=message.case_id,
            msg_id=reply.msg_id,
            causation_id=message.msg_id,
            **{"from": agent.name},
            to=message.sender,
            evidence_count=len(report.evidence),
            degraded=report.degraded,
            notes=report.notes,
            facts=summarize(report.facts),
        )
        return reply
