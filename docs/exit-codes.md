# Exit Codes

`ohbs-image` promises a stable exit-code contract so scripts and CI gates can
depend on the meaning of a non-zero status. Codes below are part of the CLI
contract and will not change within a minor version; additions are announced
in the changelog.

## Global contract

| Code | Meaning |
|------|---------|
| `0`  | **Ready / success** — the command completed and its gates passed |
| `1`  | **Blocked** — a check or gate failed (diagnostics detail in output) |
| `2`  | **Configuration error** — the TOML could not be loaded or resolved |
| `70` | **Internal error** — unexpected exception (re-run with `-v` for a traceback) |
| `130`| Interrupted by the user (Ctrl-C) |
| `255`| Packer subprocess failure propagated for build-type commands |

## Per-command behaviour

### Workflow commands

| Command | Notes |
|---------|-------|
| `doctor` | `0` ready · `1` one or more checks blocked · `2` configuration could not be resolved |
| `plan` | `0` plan generated · `1` with `--check`, high-risk settings detected · `2` configuration error |
| `preflight` | `0` pass · `1` fail |
| `validate` | `0` packer validate passed · `1` fail · `2` configuration error |
| `build` | `0` image built and gates passed · `1` build/gate failure · `2` configuration error |
| `scan` | `0` score gate passed · `1` score below gate or build failure |
| `test` | `0` idempotency clean · `1` changes detected or build failure |
| `configure` / `init` | `0` written · `1` interactive selection cancelled or failure |

### Evidence and management commands

| Command | Notes |
|---------|-------|
| `state sync` | `0` synced · `1` backend failure (or `--check` on a non-local backend) |
| `state path` | always `0` |
| `state status` | always `0` |
| `state init` | `0` layout ready · `1` filesystem failure |
| `state prune` | `0` pruned or previewed · `1` no criteria given or rewrite failure |
| `engine list` / `engine version` | always `0` |
| `engine verify` | `0` all bundled engines valid · `1` one or more engines broken |
| `config schema` / `explain` | `0` ok · `1` key not documented or key missing |
| `config validate` | `0` valid · `1` invalid configuration · `2` file missing/unreadable |
| `config diff` | `0` identical · `1` differences found (or either file unreadable) |
| `config get` | `0` printed · `1` unknown key or unreadable config |
| `config migrate` | `0` migrated or already current · `1` could not read the file |
| `report diff` | `0` compared · `1` lineage or run ID missing |
| `list` | always `0` |
| `verify` / `verify-image` / `verify-release` | `0` verified · `1` verification failed |
| `audit` | `0` score gate passed · `1` below gate or tool failure |
| `cleanup-images` / `cleanup-runs` | `0` done · `1` failure (both dry-run by default) |

### Release commands

| Command | Notes |
|---------|-------|
| `promote` / `rollback` | `0` recorded · `1` evidence or input invalid |
| `verify-release` | `0` evidence complete · `1` evidence missing or mismatched |

## Policy

- Exit codes are **stable**: scripts may branch on `0` vs non-zero, and on the
  documented `2` configuration split.
- `doctor` distinguishes configuration errors (`2`) from blocked readiness
  (`1`) so CI can decide whether a failure is fixable at runtime.
- `plan --check` is the recommended CI gate for review pipelines: it exits
  non-zero only when the plan contains high-risk settings, so a read-only
  plan can gate a pull request without touching the cloud.
