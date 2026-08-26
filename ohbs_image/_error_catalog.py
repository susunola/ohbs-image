from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorDefinition:
    status: int
    retryable: bool
    action: str


ERROR_CATALOG: dict[str, ErrorDefinition] = {
    "invalid_request": ErrorDefinition(400, False, "Correct the request using the validation message."),
    "idempotency_required": ErrorDefinition(400, False, "Retry with a stable Idempotency-Key header."),
    "cost_confirmation_required": ErrorDefinition(400, False, "Confirm the billable operation explicitly."),
    "forbidden": ErrorDefinition(403, False, "Request the required role or bucket scope."),
    "not_found": ErrorDefinition(404, False, "Verify the route and resource identifier."),
    "payload_too_large": ErrorDefinition(413, False, "Reduce the request body below the configured limit."),
    "rate_limited": ErrorDefinition(429, True, "Retry with backoff after the rate-limit window."),
    "internal_error": ErrorDefinition(500, True, "Capture a support bundle and retry once."),
}


def error_document(code: str, message: str, *, status: int | None = None) -> dict[str, object]:
    definition = ERROR_CATALOG.get(code)
    if definition is None:
        code = "internal_error"
        definition = ERROR_CATALOG[code]
    effective_status = definition.status if status is None else status
    if effective_status != definition.status:
        raise ValueError(
            f"error code {code!r} requires HTTP {definition.status}, got {effective_status}"
        )
    return {
        "code": code,
        "message": message,
        "retryable": definition.retryable,
        "action": definition.action,
        "documentation": f"https://ohbs-image.dev/errors/{code}",
    }
