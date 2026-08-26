# OPA admission policy

Generate admission input from synchronized ohbs-image state, then evaluate it:

```bash
ohbs-image consumer resolve rhel10 stable \
  --policy docs/policy-bundle.example.json \
  --environment production > admission.json

opa eval --fail-defined \
  --data integrations/opa/ohbs_image_admission.rego \
  --input admission.json 'data.ohbs_image.admission.deny[_]'

opa eval --fail \
  --data integrations/opa/ohbs_image_admission.rego \
  --input admission.json 'data.ohbs_image.admission.allow'
```

The first command is the authoritative resolver: it fails closed for missing,
tampered, cross-bucket, quarantined, or revoked artifacts. Rego provides a second
deployment-side guard over the portable `consumer-admission/v1` document.
