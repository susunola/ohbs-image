# Public benchmark

The benchmark is local, deterministic and free of cloud API calls. It measures
four controller hot paths: canonical evidence hashing, transactional registry
upserts, indexed registry search, and provider protocol validation.

Run it with:

```sh
ohbs-image benchmark run --iterations 500 --output benchmark.json
ohbs-image benchmark compare benchmark.json baseline.json --max-regression-percent 20
```

Results include Python, operating system and CPU architecture metadata. Compare
results only on equivalent runners. The median is the regression signal; p95
and throughput are diagnostic. CI publishes the raw JSON artifact on every
change to benchmark-sensitive code. This benchmark deliberately makes no
claims about cloud provisioning speed, which depends on provider capacity,
region, image size and network conditions.
