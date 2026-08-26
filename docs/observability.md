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
