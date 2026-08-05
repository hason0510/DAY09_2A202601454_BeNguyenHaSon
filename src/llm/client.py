"""Thin OpenAI wrapper: JSON-only, deterministic, never fatal.

Contract with the rest of the system: this returns a dict or None. A None means
"the LLM had nothing usable to say" and every caller must already have a
deterministic answer to fall back on. No number in the graded output is ever
produced here.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

from .. import config


class LLMClient:
    def __init__(self, tracer=None, enabled: bool = True) -> None:
        self._tracer = tracer
        self._lock = threading.Lock()
        self.calls = 0
        self.failures = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.enabled = enabled
        self._client = None

        if not enabled:
            return
        if not config.api_key():
            self.enabled = False
            return
        try:
            from openai import OpenAI
        except ImportError:
            self.enabled = False
            return
        kwargs: dict[str, Any] = {"api_key": config.api_key(), "timeout": config.REQUEST_TIMEOUT_S}
        if config.base_url():
            kwargs["base_url"] = config.base_url()
        self._client = OpenAI(**kwargs)

    def ask_json(
        self,
        *,
        system: str,
        user: str,
        purpose: str,
        case_id: str | None = None,
    ) -> dict | None:
        """One deterministic JSON-mode completion, retried on transient errors."""
        if not self.enabled or self._client is None:
            return None

        last_error = ""
        for attempt in range(1, config.MAX_RETRIES + 1):
            started = time.perf_counter()
            try:
                resp = self._client.chat.completions.create(
                    model=config.MODEL_NAME,
                    temperature=config.TEMPERATURE,
                    seed=config.SEED,
                    max_tokens=config.MAX_OUTPUT_TOKENS,
                    response_format={"type": "json_object"},
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                )
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                self._note_failure(purpose, case_id, last_error, attempt)
                time.sleep(min(2**attempt, 8))
                continue

            latency_ms = round((time.perf_counter() - started) * 1000, 1)
            content = (resp.choices[0].message.content or "").strip()
            usage = getattr(resp, "usage", None)
            with self._lock:
                self.calls += 1
                if usage is not None:
                    self.prompt_tokens += usage.prompt_tokens or 0
                    self.completion_tokens += usage.completion_tokens or 0

            try:
                parsed = json.loads(content)
            except json.JSONDecodeError:
                last_error = "response was not valid JSON"
                self._note_failure(purpose, case_id, last_error, attempt)
                continue

            if self._tracer:
                self._tracer.emit(
                    "llm_call",
                    case_id=case_id,
                    purpose=purpose,
                    model=config.MODEL_NAME,
                    latency_ms=latency_ms,
                    prompt_tokens=getattr(usage, "prompt_tokens", None),
                    completion_tokens=getattr(usage, "completion_tokens", None),
                    response=parsed,
                )
            return parsed if isinstance(parsed, dict) else None

        with self._lock:
            self.failures += 1
        if self._tracer:
            self._tracer.emit(
                "llm_unavailable", case_id=case_id, purpose=purpose, error=last_error
            )
        return None

    def _note_failure(self, purpose: str, case_id: str | None, error: str, attempt: int) -> None:
        if self._tracer:
            self._tracer.emit(
                "llm_retry", case_id=case_id, purpose=purpose, attempt=attempt, error=error
            )

    @property
    def usage(self) -> dict[str, int | bool]:
        return {
            "enabled": self.enabled,
            "calls": self.calls,
            "failed_calls": self.failures,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
        }
