# Consumer compatibility contract

| Consumer | Supported contract | Failure behavior |
|---|---|---|
| Terraform native provider | Terraform 1.5+, Plugin Protocol 6, control API v1 | Read fails for API/auth/inactive image errors |
| Terraform external module | Terraform 1.5+, `consumer-admission/v1` | Plan precondition blocks denied admission |
| GitHub Actions | `consumer-admission/v1`, uploaded evidence | Job exits non-zero before deployment |
| GitLab CI | `consumer-admission/v1`, dotenv image ID | Admission stage blocks downstream stages |
| OPA | Rego v1, `consumer-admission/v1` | Default deny |
| GitOps | `golden-image-lock/v1` | Generation/hash drift blocks reconciliation |

Compatibility rules:

1. Existing fields in a versioned schema retain their meaning and type.
2. Consumers reject unknown schema versions and inactive artifacts.
3. Mutable channels are resolved during review; deployments consume immutable
   artifact IDs plus channel generation.
4. Admission evidence and locks are integrity-bound by SHA-256 hashes.
5. Breaking changes require a new schema/API version and a parallel migration
   window; they are never silently introduced into `/api/v1`.
