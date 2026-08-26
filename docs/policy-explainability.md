# Policy explainability

Inspect the effective policy before evaluating or publishing it:

```bash
ohbs-image policy explain organization-policy.json \
  --environment production \
  --artifact-id IMAGE_ID \
  --output json
```

The explanation resolves inheritance first, then reports every effective
control with its source:

- `defaults` means the organisation-wide value applies;
- `environments.production` means the environment overrides the default;
- exceptions are labelled `active`, `expired`, or `not_applicable` for the
  requested artifact and environment.

The command is read-only. Its output contains a SHA-256 `document_hash`, so an
approval or audit record can bind itself to the exact explanation reviewed by
the operator. Use `policy check` afterwards to compare the effective
requirements with the artifact's actual evidence.
