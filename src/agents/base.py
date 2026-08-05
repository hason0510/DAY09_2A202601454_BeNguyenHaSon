"""Agent base class.

An agent declares three things and nothing else is negotiable:
  * ``name``          -- its address on the bus
  * ``capabilities``  -- the CSV scopes it may read (enforced by views.py)
  * ``uses_llm``      -- whether it consumes tokens (rendered in metadata.json)
"""

from __future__ import annotations

from ..a2a.message import AgentReport, Message
from ..store.views import DataView


class Agent:
    name: str = "agent"
    role: str = ""
    capabilities: frozenset[str] = frozenset()
    uses_llm: bool = False

    def handle(self, message: Message, view: DataView) -> AgentReport:  # pragma: no cover
        raise NotImplementedError
