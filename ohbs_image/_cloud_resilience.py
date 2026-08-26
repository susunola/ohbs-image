from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._failures import Failure, FailureCategory, classify_failure
from ._reports import _state_lock


@dataclass
class CircuitState:
    failures: int = 0
    opened_at: float = 0.0


class CircuitBreaker:
    """Thread-safe closed/open/half-open breaker for provider operations."""

    def __init__(self, *, threshold: int = 5, reset_seconds: float = 60.0) -> None:
        if threshold < 1 or reset_seconds <= 0:
            raise ValueError("circuit breaker settings must be positive")
        self.threshold = threshold
        self.reset_seconds = reset_seconds
        self._states: dict[str, CircuitState] = {}
        self._lock = threading.Lock()

    def allow(self, operation: str, *, now: float | None = None) -> bool:
        current = now if now is not None else time.monotonic()
        with self._lock:
            state = self._states.get(operation, CircuitState())
            return state.failures < self.threshold or current - state.opened_at >= self.reset_seconds

    def success(self, operation: str) -> None:
        with self._lock:
            self._states.pop(operation, None)

    def failure(self, operation: str, *, now: float | None = None) -> None:
        current = now if now is not None else time.monotonic()
        with self._lock:
            state = self._states.setdefault(operation, CircuitState())
            state.failures += 1
            if state.failures >= self.threshold:
                state.opened_at = current


def classify_provider_error(code: str, message: str, *, phase: str = "") -> Failure:
    combined = f"{code}: {message}"
    normalized = code.lower()
    if "resourceinsufficient" in normalized or "soldout" in normalized:
        return Failure(FailureCategory.CAPACITY, False, "capacity-unavailable",
                       combined[:240], phase)
    if "requestlimit" in normalized or "ratelimit" in normalized:
        return Failure(FailureCategory.RATE_LIMIT, True, "rate-limited",
                       combined[:240], phase)
    if "internalerror" in normalized or "serviceunavailable" in normalized:
        return Failure(FailureCategory.PROVIDER, True, "provider-transient",
                       combined[:240], phase)
    return classify_failure(combined, phase=phase)


def record_takeover(path: Path, *, operation: str, failure: Failure,
                    attempts: int) -> None:
    """Append a secret-free terminal failure for operator takeover."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    event: dict[str, Any] = {"operation": operation, "attempts": attempts,
        "failure": failure.to_dict(), "requires_manual_takeover": True}
    lock = _state_lock(path)
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
    finally:
        lock.rmdir()


PROVIDER_BREAKER = CircuitBreaker()
