# Core contract stability

The stable product boundary is recorded in `contracts/core-contracts.json`.
It covers top-level CLI commands, OpenAPI operation IDs, versioned JSON Schema
files, provider and extension entry points, capabilities, and public evidence
schema identifiers.

CI runs:

```bash
python3 scripts/check_core_contracts.py
```

When a compatible contract is intentionally extended, review
`schemas/COMPATIBILITY.md`, update tests and documentation, then explicitly
accept the new snapshot:

```bash
python3 scripts/check_core_contracts.py --update
```

Do not update the snapshot to silence a failure. Removing commands, narrowing
schemas, changing meanings, or breaking provider/extension protocols requires
a new major contract version and a documented migration path.
