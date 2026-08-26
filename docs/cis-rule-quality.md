# CIS rule quality governance

Rule count is not a quality metric. The quality gate evaluates whether each
rule is structurally complete, semantically consistent, explainable,
implemented, and supported by useful evidence.

Generate the full baseline without blocking on legacy semantic conflicts:

```bash
ohbs-image catalog lint --report cis-rule-quality.html
ohbs-image catalog lint --output json > cis-rule-quality.json
```

Use strict mode in remediation branches and future release gates:

```bash
ohbs-image catalog lint --strict --profile rhel10 --output json
```

Strict mode fails when a rule is structurally invalid or contradicts its own
automation declaration—for example, `assessment=Automated` with
`family=manual` or `automated=false`. Relaxed mode keeps legacy semantic
conflicts visible as warnings so the baseline can be generated before the
catalog is repaired.

## Quality dimensions

Each rule receives a machine-readable record conforming to
[`rule-quality.schema.json`](../schemas/v1/rule-quality.schema.json):

- required catalog metadata;
- guidance presence;
- rationale;
- remediation or remediation hint;
- documented audit method;
- operational impact;
- meaningful risk classification;
- implemented, enabled automation family;
- rollback instructions.

Grades summarize evidence completeness: A is at least 87.5%, B at least 75%,
C at least 50%, and D is below 50%. A grade does not assert CIS certification
or prove that remediation is safe on every workload.

## Remediation order

The HTML report ranks 50 rules using disruptive risk, automation status,
missing dimensions, and lint errors. Review these rules first, then add
before/remediate/after/idempotency fixtures and rollback evidence. Do not lower
the lint standard simply to make the current baseline green.
