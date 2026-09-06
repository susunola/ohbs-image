# Observability

Every CLI command and control-plane request emits a W3C-correlated span to
`<state>/telemetry/traces.jsonl`. Incoming HTTP `traceparent` headers continue
the caller's trace. Set `OHBS_IMAGE_OTLP_ENDPOINT` to push trace spans using
OTLP/HTTP JSON; exporter failures are isolated from production work and written
to `telemetry/export-errors.jsonl`.

Record and push metric snapshots independently:

```bash
ohbs-image report metrics --format otlp-json --record
ohbs-image report metrics --record --push https://otel-collector.example:4318
ohbs-image report trends --limit 30
```

The trend database uses SQLite/WAL and retains full metric snapshots so success
rate, retry rate, duration, failure categories, artifact states, channels and
replica states can be compared over time. Authentication and TLS for the OTLP
collector should be enforced by a local collector or trusted reverse proxy;
credentials are not accepted on command lines or persisted in trace records.

Native Tencent Cloud builds additionally retain per-call provider evidence in
`<state>/native/build-record.json`. `provider_evidence.api_requests` contains
the action, region, RequestId, total duration, actual attempt count and final
status for each logical API operation. `provider_evidence.api_summary` provides
call/failure/attempt/retry counts, total/P95/max latency and the five slowest
operations. Failure events store only the exception class, never exception text,
request parameters, credentials or signed headers.

`provider_evidence.transfer_cache` reports the Native Engine's content-transfer
economics: eligible files, verified hits, misses, bytes uploaded and bytes
saved. Only the deterministic OHBS-generated Ansible archive is eligible for
the persistent image cache. Cache names are full SHA-256 digests and every hit
is re-hashed before use; arbitrary file and shell provisioners always bypass it.

Native journals refresh an atomic heartbeat every 15 seconds while lifecycle
work is active. The update preserves the current phase, status, run/plan
identity and completed provisioners. Build evidence reports heartbeat writes
and write errors. Failed records also carry a secret-free structured failure
with category, stable code, retryability, phase and exception type; raw
exception text remains in the local build log rather than the machine contract.
When phase caps are configured, `phase_budget_seconds` records the effective
budget at each transition. This makes timeout classification auditable and
distinguishes a deliberately bounded phase from exhaustion of the global build
deadline.
