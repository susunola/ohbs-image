# Public evidence index

`scripts/build_evidence_index.py` turns portable workflow JSON into a
self-contained HTML index and a stable machine-readable JSON document. It is
intended for GitHub Pages, release attachments, audit hand-offs, or any static
web host; it does not require the control-plane service.

```bash
python3 scripts/build_evidence_index.py evidence/ \
  --output-html public-evidence-index.html \
  --output-json public-evidence-index.json
```

The collector recognizes real-cloud acceptance results, production proof,
run SLO, benchmark, release, and compliance documents. Every row includes the
source SHA-256. Missing files, malformed JSON, incomplete observation windows,
and synthetic benchmarks are never promoted to successful production proof.

Use `--generated-at` in reproducibility tests. In production, omit it to record
the current UTC time. Linked workflow or release URLs remain the authoritative
source; consumers should verify signed releases and hashes before relying on a
claim.
