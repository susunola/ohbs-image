# GitOps image lock contract

Commit a lock document conforming to `golden-image-lock.schema.json` beside the
workload. Automation proposes generation bumps; review and policy checks happen
in the pull request. Deployment controllers consume the immutable artifact ID,
not a mutable channel name. A stale generation or changed admission hash must
fail the pull request rather than silently selecting a different image.
