"""Run trace writer.

README section 8 wants a real trace of the 50-case run, latest run only (never
appended). We truncate the file once at run start, then every bus hop, LLM call
and verifier verdict appends one JSON line. Writes are mutex-guarded because
cases run concurrently.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any


class TraceWriter:
    def __init__(self, path: Path, run_id: str) -> None:
        self.path = path
        self.run_id = run_id
        self._lock = threading.Lock()
        self._seq = 0
        self._counts: dict[str, int] = {}
        path.parent.mkdir(parents=True, exist_ok=True)
        # Truncate: "khong append, chi can luot chay moi nhat".
        self._fh = path.open("w", encoding="utf-8")

    def emit(self, event: str, case_id: str | None = None, **fields: Any) -> None:
        with self._lock:
            self._seq += 1
            self._counts[event] = self._counts.get(event, 0) + 1
            record = {
                "seq": self._seq,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "run_id": self.run_id,
                "event": event,
                "case_id": case_id,
                **fields,
            }
            self._fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            self._fh.flush()

    @property
    def counts(self) -> dict[str, int]:
        return dict(self._counts)

    @property
    def lines(self) -> int:
        return self._seq

    def close(self) -> None:
        with self._lock:
            self._fh.close()


def summarize(payload: dict[str, Any], limit: int = 400) -> Any:
    """Keep the trace readable: long payloads are summarized, not dumped."""
    try:
        text = json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(payload)[:limit]
    if len(text) <= limit:
        return payload
    return {"_truncated": True, "_bytes": len(text), "_head": text[:limit]}
