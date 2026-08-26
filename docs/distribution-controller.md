# Distribution controller

Queue regional copies without cloud mutation:

```bash
ohbs-image distribution enqueue img-123 --region ap-shanghai
ohbs-image distribution worker --once                 # dry-run queue inspection
ohbs-image distribution worker --apply --global-limit 8 --account-limit 4 --region-limit 2
```

Cross-account delivery uses Tencent Cloud image sharing in the source region:

```bash
ohbs-image distribution enqueue img-123 --region ap-guangzhou \
  --mode share --account 103849387508
```

The share worker uses the source-account `TENCENTCLOUD_*` credentials. Regional
sync jobs use `TENCENTCLOUD_*` for account `self`, or
`<ACCOUNT>_SECRET_ID` / `<ACCOUNT>_SECRET_KEY` for named account profiles.
Secrets are never persisted in queue documents.

Run reconciliation and SLO checks independently:

```bash
ohbs-image distribution reconcile-all --apply --interval-seconds 60
ohbs-image distribution slo --target-minutes 30
```

`SyncImages` copies custom images across regions. `ModifyImageSharePermission`
shares an image with another root account in the same source region. Queue
leases recover abandoned work; quota checks are made in the same SQLite
transaction as claims.
