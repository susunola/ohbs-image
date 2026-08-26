# Production control-plane boundary

The built-in HTTP server is an application server. Keep it on loopback and put
it behind a maintained TLS reverse proxy. `deploy/Caddyfile.example` and
`deploy/ohbs-image.service` provide a hardened starting point.

## Required controls

1. Run as a dedicated unprivileged account with a private state directory.
2. Terminate TLS at Caddy, an ingress controller, or the organization's gateway.
3. Keep `--max-body-bytes`, `--request-timeout`, and per-token rate limiting on.
4. Probe `/healthz` for liveness and `/readyz` for readiness.
5. Rotate tokens by atomically replacing `rbac.json`; the service reloads it
   without restart. Set `expires_at` on every non-emergency token.
6. Send structured access logs and Prometheus metrics to the central platform.
7. Restrict the reverse proxy to trusted networks or an identity-aware proxy.

An RBAC principal can expire automatically:

```json
{
  "tokens": {
    "replace-with-a-secret": {
      "subject": "release-bot",
      "roles": ["viewer", "promoter"],
      "buckets": ["tencentos3"],
      "expires_at": "2026-09-30T00:00:00Z"
    }
  }
}
```

Do not place tokens in command-line arguments, URLs, browser storage, or Git.
