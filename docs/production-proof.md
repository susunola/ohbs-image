# Production proof ledger

`ohbs-image proof record` appends one daily, hash-chained snapshot containing
real run SLOs plus a reproducible synthetic registry/backup/recovery benchmark.
Duplicate dates and corrupt chains fail closed.

Run it daily from a persistent state volume, then evaluate claims with:

```sh
ohbs-image proof verify
ohbs-image proof report --days 30 --html proof-30d.html
```

The report refuses to claim completion until it has 30 distinct days, a valid
ledger, at least 98% terminal-run success and a successful recovery check on
every recorded day. A 90-day report uses the same rules. Synthetic benchmark
throughput is labelled synthetic and never presented as cloud performance.
