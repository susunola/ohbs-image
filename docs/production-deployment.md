# Production deployment and rollback

Run the Console behind TLS and an identity-aware proxy. Port 8181 is the
application boundary; do not expose it directly to the internet. Store the
RBAC document and cloud credentials in a secret manager, mount them read-only,
and keep state on encrypted persistent storage.

## Pre-upgrade

1. Run `ohbs-image upgrade check TARGET --database PATH`.
2. Run `ohbs-image state db verify` and create an online backup with
   `ohbs-image state db backup BACKUP_PATH`.
3. Verify downloaded artifacts with GitHub provenance attestations and the
   published SHA256 checksums.
4. Deploy the immutable image digest to one canary and check `/readyz`, API
   queries and one non-billed dry run before updating the remaining instance.

## Rollback

Stop writes, deploy the previously recorded package/container digest, and
restore the pre-upgrade database backup only if the release changed its schema.
Run `state db verify`, then resume traffic. Never run two versions against the
same SQLite volume. High availability remains intentionally out of scope for
this single-writer deployment package.

The `deploy/` directory contains systemd units, a Compose example and a
single-replica Kubernetes baseline. Replace example RBAC values before use.
