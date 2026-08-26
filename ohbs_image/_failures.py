from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any


class FailureCategory(StrEnum):
    NETWORK = "network"
    RATE_LIMIT = "rate_limit"
    CAPACITY = "capacity"
    TIMEOUT = "timeout"
    AUTH = "auth"
    CONFIGURATION = "configuration"
    POLICY = "policy"
    SECURITY_GATE = "security_gate"
    PROVIDER = "provider"
    INTERNAL = "internal"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Failure:
    category: FailureCategory
    retryable: bool
    code: str
    summary: str
    phase: str = ""

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["category"] = self.category.value
        return value


_RULES: tuple[tuple[FailureCategory, bool, str, re.Pattern[str]], ...] = (
    (FailureCategory.SECURITY_GATE, False, "security-gate",
     re.compile(r"clean.?boot.*fail|attestation.*unsigned|vulnerabilit|CVE.*gate", re.I)),
    (FailureCategory.POLICY, False, "policy-denied",
     re.compile(r"score.*below|release.*blocked|scoped.*approval|policy.*den", re.I)),
    (FailureCategory.AUTH, False, "authentication",
     re.compile(r"unauthori[sz]ed|access.?denied|auth(?:entication)? fail|credential.*(?:missing|invalid)", re.I)),
    (FailureCategory.CONFIGURATION, False, "invalid-configuration",
     re.compile(r"invalid config|unknown argument|unsupported|parse error|syntax error|placeholder", re.I)),
    (FailureCategory.RATE_LIMIT, True, "rate-limited",
     re.compile(r"rate.?limit|requestlimit|too many requests|HTTP 429", re.I)),
    (FailureCategory.CAPACITY, True, "capacity-unavailable",
     re.compile(r"insufficient.*capacity|resource.*sold.?out|instance.*unavailable|no stock", re.I)),
    (FailureCategory.TIMEOUT, True, "operation-timeout",
     re.compile(r"timed? out|time budget exhausted|deadline exceeded", re.I)),
    (FailureCategory.NETWORK, True, "network-transient",
     re.compile(r"connection reset|temporary failure|network is unreachable|DNS|TLS|SSL|EOF|502|503|504", re.I)),
    (FailureCategory.PROVIDER, True, "provider-transient",
     re.compile(r"internalerror|service unavailable|gateway error|HTTP 5\d\d", re.I)),
    (FailureCategory.INTERNAL, False, "internal-error",
     re.compile(r"traceback|assertionerror|internal error", re.I)),
)


def classify_failure(message: str, *, phase: str = "") -> Failure:
    text = message.strip()
    for category, retryable, code, pattern in _RULES:
        if pattern.search(text):
            return Failure(category, retryable, code, text[:240] or code, phase)
    return Failure(FailureCategory.UNKNOWN, False, "unknown", text[:240] or "unknown failure", phase)


def retry_delay(attempt: int, *, base_seconds: float = 1.0,
                maximum_seconds: float = 30.0) -> float:
    """Deterministic capped exponential delay; attempt is one-based."""
    return float(min(maximum_seconds, base_seconds * (2 ** max(0, attempt - 1))))
