# Machine-readable error catalog

Control API failures use a stable envelope:

```json
{
  "error": {
    "code": "rate_limited",
    "message": "request rate limit exceeded",
    "retryable": true,
    "action": "Retry with backoff after the rate-limit window.",
    "documentation": "https://ohbs-image.dev/errors/rate_limited"
  }
}
```

Clients must branch on `error.code`, never parse `message`. New codes may be
added within API v1; the meaning, HTTP status, and retryability of an existing
code will not change within that major version.

Authenticated viewers can retrieve the live catalog from
`GET /api/v1/errors`. Current codes are:

| Code | HTTP | Retryable | Client response |
|---|---:|:---:|---|
| `invalid_request` | 400 | no | Correct the request using its validation message. |
| `idempotency_required` | 400 | no | Supply a stable `Idempotency-Key`. |
| `cost_confirmation_required` | 400 | no | Explicitly confirm the billable operation. |
| `forbidden` | 403 | no | Request the required role or Bucket scope. |
| `not_found` | 404 | no | Verify the route and resource identifier. |
| `payload_too_large` | 413 | no | Reduce the request body. |
| `rate_limited` | 429 | yes | Retry with bounded exponential backoff. |
| `internal_error` | 500 | yes | Capture a support bundle and retry once. |

CLI process exit codes remain a separate contract documented in
[Exit Codes](exit-codes.md).
