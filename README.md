
<p align="center">
  <img src="docs/ohbs-image-logo.png" alt="ohbs-image" width="440">
</p>

<p align="center">
  <b>English</b> &nbsp;|&nbsp;
  <a href="README.zh-CN.md">简体中文</a> &nbsp;|&nbsp;
  <a href="README.ja.md">日本語</a> &nbsp;|&nbsp;
  <a href="README.th.md">ภาษาไทย</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/version-0.19.1-blue?logo=pypi&logoColor=white" alt="Version 0.19.1">
  <img src="https://img.shields.io/badge/python-3.11_|_3.12_|_3.13_|_3.14-blue?logo=python&logoColor=white" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/profiles-13-orange" alt="13 profiles">
  <img src="https://img.shields.io/badge/platform-Tencent%20Cloud-0052D9" alt="Tencent Cloud">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="License: MIT">
  <a href="https://github.com/susunola/ohbs-image/actions/workflows/ci.yml"><img src="https://github.com/susunola/ohbs-image/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
</p>

<p align="center">
  <a href="https://github.com/susunola/ohbs-image"><img src="https://img.shields.io/badge/ohbs--image-golden%20image%20builder-1f6feb?style=flat-square&logo=packer&logoColor=white" alt="ohbs-image — golden image builder"></a>&nbsp;
  <a href="https://github.com/susunola/ohbs-host"><img src="https://img.shields.io/badge/ohbs--host-CIS%20Benchmark%20CLI-1f6feb?style=flat-square&logo=ansible&logoColor=white" alt="ohbs-host — CIS Benchmark CLI"></a>&nbsp;
  <a href="https://github.com/susunola/ohbs-cloud"><img src="https://img.shields.io/badge/ohbs--cloud-Terraform%20policy-1f6feb?style=flat-square&logo=terraform&logoColor=white" alt="ohbs-cloud — Terraform hardening policy"></a>
</p>

<p align="center">
  Part of the <strong><a href="https://github.com/susunola">oh baseline</a> hardening suite</strong> — 生死看淡，不服就干
</p>

# oh baseline image

> **Repository / CLI / package:** `ohbs-image` · Full name: **oh baseline image** — part of the **oh baseline** (ohbs) family.
**Config-driven golden-image builder for Tencent Cloud.** ohbs-image launches a short-lived CVM, applies CIS hardening from its bundled ohbs-os engine, re-audits against a configurable score gate, and captures the result as a custom image — fully repeatable and auditable, every time. Built for DevOps and security teams that need hardened base images they can trust in CI pipelines, Auto Scaling launch templates, and Terraform image references.

Zero pip dependencies. 13 OS profiles across Linux and Windows. Build-time gate with configurable score threshold. All roles ship inside the package — no Galaxy, no network drift.

Beyond the build itself, ohbs-image covers the full **build → test → distribute** governance loop:

- **Instance-level smoke test** before the snapshot — a broken image never ships
- **Image lineage** (`ohbs-image images`) — source → image IDs, score, version history
- **WeCom notifications** — pair with cron/systemd timer for scheduled rebuilds
- **SLSA-style signed provenance** (`ohbs-image verify provenance`) — tamper-evident build records
- **OIDC / STS credentials** — zero long-lived AK/SK in CI; `assume_role` for group accounts

---

## Table of Contents

- [Quick Start](#quick-start)
- [Installation](#installation)
- [Commands](#commands)
- [Configuration](#configuration)
- [Architecture](#architecture)
- [Profiles](#profiles)
- [Test Matrix](#test-matrix)
- [CI/CD Integration](#ci-cd-integration)
- [Security model (for enterprise review)](#security-model-for-enterprise-review)
- [Group accounts (organization)](#group-accounts-organization)
- [Troubleshooting](#troubleshooting)
- [Roadmap](#roadmap)
- [Contributing](#contributing)
- [CIS Benchmarks Disclaimer](#cis-benchmarks-disclaimer)
- [License](#license)

---

## Quick Start

### Zero-to-first-image in one command (recommended)

```bash
export TENCENTCLOUD_SECRET_ID=AKIDxxxx
export TENCENTCLOUD_SECRET_KEY=xxxx

ohbs-image quickstart --region ap-guangzhou --profile ubuntu2204
# 1. discovers the newest Ubuntu 22.04 public image + an in-stock instance type
# 2. provisions temporary VPC/subnet/security-group (tagged ephemeral=true)
# 3. writes ohbs-image.toml, then runs doctor + plan
# 4. prints the exact build command — or pass --yes to chain build directly

ohbs-image build --config ohbs-image.toml   # produce the hardened image
```

`quickstart` reuse the networking you already have by passing all three of
`--vpc`, `--subnet`, `--security-group` (never carves a subnet inside someone
else's VPC).  Temporary resources are recorded in `ohbs-image.toml.quickstart.json`
and torn down with `ohbs-image quickstart --cleanup`.  Preview everything
read-only first with `ohbs-image quickstart --region … --dry-run`.

Prefer the manual path below when you want to learn the configuration model
first, or when your networking is shared and you need explicit choices.

### Manual path (learn the config model)

```bash
# 1. Install
git clone https://github.com/susunola/ohbs-image.git
cd ohbs-image
pip install .

# 2. Generate a minimal configuration (interactive; flags also supported)
ohbs-image configure

# 3. Build
ohbs-image doctor      # actionable local/config/cloud diagnosis
ohbs-image plan        # read-only resource, gate, duration, and cost preview
ohbs-image validate    # dry-run: render templates + packer validate
ohbs-image build       # produce the hardened custom image
ohbs-image clean       # remove build artifacts
```

> **No Tencent Cloud account yet? Try before you build — free.**
> `ohbs-image try` runs the same engine + catalog gates CI runs, then renders
> a sample single-page HTML compliance report, entirely offline:
>
> ```bash
> ohbs-image try                 # demo report in ./ohbs-image-try/
> # ...or inside the shipped container image:
> docker build --target try -t ohbs-image:try . && \
> docker run --rm -v "$(pwd)/out:/demo/out" ohbs-image:try
> ```
>
> It touches no cloud resources and spends nothing; a real `build` adds the
> ephemeral CVM, remediation, clean-boot verification and signed provenance.

```bash
# Set credentials (environment variables only — never in config files)
export TENCENTCLOUD_SECRET_ID=AKIDxxxx
export TENCENTCLOUD_SECRET_KEY=xxxx
export WINRM_PASSWORD=xxxx   # Windows builds only
```

**Example output (`build`)**

```
══════════════════════════════════════════════════════════
  ohbs-image 0.19.1 — tencentos3 (L1) → ap-guangzhou-4
══════════════════════════════════════════════════════════
[packer]  tencentcloud-cvm: Launching instance (S5.MEDIUM2)...
[packer]  tencentcloud-cvm: Provisioning with ansible-local...
[packer]      tencentcloud-cvm: TASK [cis-tencentos3 : apply CIS Level 1] ***
[packer]      tencentcloud-cvm: ok: 142  changed: 38  failed: 0
[packer]      tencentcloud-cvm: TASK [cis-tencentos3 : reboot] ************
[packer]      tencentcloud-cvm: Instance rebooted — re-auditing pending items
[packer]      tencentcloud-cvm: TASK [cis-tencentos3 : gate] **************
[packer]      tencentcloud-cvm:
[packer]      tencentcloud-cvm: ═══ CIS Hardening Results ═══
[packer]      tencentcloud-cvm: Mode:      apply
[packer]      tencentcloud-cvm: Profile:   L1
[packer]      tencentcloud-cvm: Total:     142
[packer]      tencentcloud-cvm: Passed:    142
[packer]      tencentcloud-cvm: Failed:    0
[packer]      tencentcloud-cvm: Score:     100% ≥ 85%  ✓ PASS
[packer]  ==> tencentcloud-cvm: smoke test: sshd config parses ... ok
[packer]  ==> tencentcloud-cvm: smoke test: /dev/shm noexec ... ok
[packer]  ==> tencentcloud-cvm: smoke test PASSED — image is buildable
[packer]  ==> tencentcloud-cvm: Creating custom image...
[packer]  ==> tencentcloud-cvm: Image created: img-abc123def456
[packer]  ==> tencentcloud-cvm: Terminating build instance...

✔  Build complete — image-id: img-abc123def456
✔  Output image ID(s): img-abc123def456
✔  Re-audit score: 100%
✔  Lineage recorded -> ~/.ohbs-image/lineage.jsonl
✔  Provenance signed with GPG key 0123ABCD -> ...provenance.json.sig
```

> **Not installed?** Replace `ohbs-image` with `python3 -m ohbs_image` in any command.

---

## Installation

### Prerequisites

| Requirement | Details |
|---|---|
| **Python** | 3.11+ (stdlib only — zero pip dependencies) |
| **Packer** | 1.12+ |
| **ansible-core** | 2.15+ (controller — required for Windows builds) |
| **ansible.windows** | `ansible-galaxy collection install -r requirements-builder.yml` (Windows builds only; version locked) |
| **Tencent Cloud** | Sub-account with `cvm:RunInstances`, `cvm:CreateImage`, `cvm:DescribeImages`; `cvm:CopyImage` for cross-region copy |
| **Network** | Dedicated VPC + subnet + security group — SSH/22 (Linux) or WinRM/5985 (Windows, NTLM message encryption), source-restricted to build machine egress IP |
| **Source Image** | Public image ID for the target OS |

### Install from source

```bash
git clone https://github.com/susunola/ohbs-image.git
cd ohbs-image
pip install .
ohbs-image --version
```

---

## Commands

### Which command should I use?

Four commands overlap at first glance; they serve four different moments of
the image lifecycle — pick by what you are trying to *do*:

| If you want to… | Use | What it does |
|---|---|---|
| Harden a source image into a **golden image** (apply remediations, re-audit, sign) | `build` | render + packer build → image + provenance + HTML delivery report |
| Check a source image against the benchmark **without changing anything** | `scan` | same engine, audit-only; score gate (default 85%); SARIF/XCCDF/HTML export |
| Independently verify with a **third-party tool** (OpenSCAP / InSpec / HardeningKitty) | `audit` | external audit tool, same `--min-score` gate, same SARIF/XCCDF export |
| Confirm a *produced* image still passes after a **clean boot** | `verify image` | boots a probe from the image and re-audits it |
| Check an already-running instance against the baseline | `drift` | config drift on a live host vs the image baseline |

`build` and `scan` run the identical bundled engine — `scan` is simply
`build` with remediation switched off, which is why its score is a fair
"before" snapshot for a migration or a quarterly compliance review.

### The five commands you will use 90% of the time

New to ohbs-image? Ignore the full reference below; start here:

```bash
ohbs-image init          # generate ohbs-image.toml (interactive)
ohbs-image preflight     # validate config + credentials before spending money
ohbs-image build         # produce the hardened golden image
ohbs-image images        # list what you have built (lineage)
ohbs-image scan          # audit-only check when you just need a score
```

Everything else — `config` tooling, `report` evidence, `state` management,
`cleanup-*` hygiene, `audit` cross-checks — is there when you need it, and
documented in the full reference below.

### Full command reference

```bash
ohbs-image                                    # show help (exits 2)
ohbs-image try [-o DIR] [--profile P] [--level 1|2]  # zero-cost offline demo: gates + sample HTML report
ohbs-image quickstart --region R --profile P  # provision temp resources + config + doctor + plan
ohbs-image quickstart --dry-run               # read-only: print the plan, create nothing
ohbs-image quickstart --cleanup               # delete resources recorded by a previous quickstart
ohbs-image init                               # generate ohbs-image.toml
ohbs-image configure                          # interactive/non-interactive minimal config generator
ohbs-image discover images --region ap-guangzhou --profile ubuntu2404
ohbs-image config schema                      # JSON Schema for editor/CI validation
ohbs-image config validate                    # local-only config check (no cloud access)
ohbs-image config diff a.toml b.toml          # field-level config comparison
ohbs-image config get ohbs.level              # effective value of one key (defaults applied)
ohbs-image config explain --all               # full configuration key reference
ohbs-image config migrate --apply             # atomic legacy config migration
ohbs-image config merge base.toml env.toml    # deep-merge layered configs (validate result)
ohbs-image report diff --before RUN --after RUN # compare lineage metadata
ohbs-image report list [--profile P] [--status ok|failed] [--limit N]
ohbs-image report show RUN_ID              # single-run evidence + run manifest
ohbs-image report html RUN_ID [-o FILE]    # re-render one run as a self-contained HTML page
ohbs-image report cost [--hourly-price USD]  # aggregate build spend from lineage facts (no billing API; spot at 10%)
ohbs-image engine list                     # bundled engines: version + sha256 per profile
ohbs-image engine verify                   # syntax-check every bundled engine (CI gate)
ohbs-image engine version                  # ohbs-image + per-family engine versions
ohbs-image catalog list                    # bundled rule catalogs: rules, guidance, sha256
ohbs-image catalog verify [--strict]       # catalog JSON + guidance cross-reference gate
ohbs-image doctor [--output text|json|sarif] [--only GROUP] [--offline] [--report-path FILE]
ohbs-image plan [--output json]                # read-only build/resource/gate preview
ohbs-image state path                          # print the evidence directory
ohbs-image state status [--output json]        # evidence counts + disk usage
ohbs-image state init                          # create the evidence layout (idempotent)
ohbs-image state prune --keep 30 [--dry-run]   # retain recent lineage, drop old per-run evidence
ohbs-image state prune --older-than 90 [--dry-run]
ohbs-image state sync push --backend local --location /shared/ohbs-state
ohbs-image state sync push --backend cos --location cos://bucket/ohbs-state
ohbs-image state sync push --backend local --location /shared/ohbs-state --check
ohbs-image preflight                          # validate config, credentials, prerequisites
ohbs-image validate                           # render templates + packer validate
ohbs-image build                              # render + packer build → custom image
ohbs-image build --skip-if-unchanged          # ... skip when inputs are unchanged (change detection)
ohbs-image scan [--min-score 85]              # audit-only build (no remediation) + score gate
ohbs-image scan --sarif out.sarif             # ... plus a SARIF 2.1.0 failure report
ohbs-image scan --xccdf out.xml               # ... plus an XCCDF 1.2 TestResult (GRC ingestion)
ohbs-image scan --html report.html            # ... plus a self-contained HTML compliance page
ohbs-image test --idempotency                 # re-run apply, fail if 2nd pass changes anything
ohbs-image list                               # enumerate available profiles with metadata
ohbs-image images [--latest] [-n N]           # list recorded builds (lineage)
ohbs-image promote --image img-xxx --environment staging --approved-by alice
ohbs-image rollback --image img-xxx --environment staging --reason "deployment issue"
ohbs-image pending                            # change detection: is a rebuild required? (exit 0/1)
ohbs-image verify provenance --provenance <file>  # verify a SLSA provenance signature
ohbs-image verify provenance --image <img-id>     # ... or locate provenance by image ID
ohbs-image verify image --image <img-id>          # clean-boot verification of a produced image
ohbs-image verify release --image img-xxx         # verify portable release-manifest evidence hashes
ohbs-image cleanup images [--older-than 30]       # retire old images by lineage age (dry run)
ohbs-image cleanup images --apply                 # actually delete (default = dry run)
ohbs-image cleanup runs --older-than 24           # find tagged orphaned build/probe CVMs (dry run)
ohbs-image cleanup runs --older-than 24 --apply   # actually terminate the tagged CVMs (hours must be > 0)
ohbs-image cleanup runs --include-legacy --apply  # explicitly include pre-manifest probes after review
# --- deprecated flat aliases (removal planned in 0.20.0) ---
ohbs-image verify --provenance <file>         # [deprecated] use `ohbs-image verify provenance`
ohbs-image verify-image --image <img-id>      # [deprecated] use `ohbs-image verify image`
ohbs-image verify-release --image img-xxx     # [deprecated] use `ohbs-image verify release`
ohbs-image cleanup-images [--older-than 30]   # [deprecated] use `ohbs-image cleanup images`
ohbs-image cleanup-runs --older-than 24       # [deprecated] use `ohbs-image cleanup runs`
ohbs-image drift --host <ip> [--image <id>]   # config drift on a running instance vs image baseline
ohbs-image drift --host <ip> --save-baseline  # save the current host scan as a drift baseline
ohbs-image check-source                       # vendor image refresh detection (rebuild needed?)
ohbs-image audit --tool oscap ...             # independent audit: OpenSCAP (RHEL-family SCAP content)
ohbs-image audit --tool inspec ...            # independent audit: Chef InSpec (dev-sec baselines)
ohbs-image audit --tool kitty --parse out.csv # independent audit: HardeningKitty (Windows) CSV
ohbs-image clean                              # remove .ohbs-image-build/
```

| Flag | Applies to | Description |
|---|---|---|
| `--config <path>` | all | Config file path (default `./ohbs-image.toml`) |
| `--overlay <toml>` | all commands that accept `--config` | Additional config file layered over `--config` (repeatable; later files win key-by-key) |
| `--workdir <dir>` | all | Build output directory (default `./.ohbs-image-build`) |
| `--state-dir <dir>` | all state-aware commands | Evidence state directory (or `OHBS_IMAGE_STATE_DIR`; it may precede the command) |
| `--quiet` | validate, build, scan | Suppress packer output |
| `--debug` | validate, build, scan | Enable `PACKER_LOG=1` |
| `-y` / `--yes` | build | Skip confirmation prompt |
| `--log-file <path>` | build | Write full build log to file |
| `--result-file <path>` | build | Write one atomic JSON result contract for CI/CD |
| `--skip-if-unchanged` | build | Skip when inputs (source image, rules, benchmark, level) are unchanged |
| `--min-score <pct>` | scan, audit, verify image | Gate threshold (default `85`; below it → exit 1) |
| `--sarif <path>` | scan, audit | Write findings as SARIF 2.1.0 |
| `--xccdf <path>` | scan, audit | Write findings as XCCDF 1.2 (enterprise GRC ingestion) |
| `--html <path>` | scan | Write a self-contained HTML compliance report (single page, no external assets) |
| `--hourly-price <usd>` | report cost | On-demand USD/hour used to estimate build spend from recorded durations; spot runs are billed at 10% (default: report the facts only, no estimate) |
| `--host <ip>` | audit | Target host to audit (oscap/inspec) |
| `--datastream <path>` | audit | oscap SCAP datastream on the target (e.g. `/usr/share/xml/scap/ssg/content/ssg-rhel9-ds.xml`) |
| `--baseline <name>` | audit | inspec baseline (default `dev-sec/linux-baseline`) |
| `--parse <csv>` | audit --tool kitty | HardeningKitty audit CSV export to parse |
| `--older-than <days>` | cleanup images | Retire builds older than N days (default `30`) |
| `--older-than <hours>` | cleanup runs | Retire tagged ephemeral CVMs older than N hours (default `24`) |
| `--include-legacy` | cleanup runs | Include old probes without a run manifest (off by default) |
| `--keep-latest <n>` | cleanup images | Always keep the newest N builds (default `1`) |
| `--unused-since <days>` | cleanup images | Only delete images NOT shared with other accounts; the in-use guard expires N days after the lineage record — older shared images are presumed unused and retired anyway (`0` = off) |
| `--apply` | cleanup images | Actually delete (default is a dry run) |

---

### First-success workflow

`configure` generates the smallest valid configuration. In a TTY it prompts
for missing values; automation can supply `--profile`, `--region`, `--zone`,
`--source-image`, `--vpc`, `--subnet`, and `--security-group`.

`doctor` returns all detected problems in one run, with a suggested fix for
each blocker. Checks are grouped into `toolchain` / `config` / `credentials` /
`cloud` / `network` / `permissions`; `--only <group>` runs a single group,
`--offline` (or `--no-cloud`) disables cloud API checks for air-gapped
machines, `--output json` emits the stable `doctor/v1` contract,
`--output sarif` emits a SARIF 2.1.0 report, and `--report-path <file>`
persists a redacted copy of the same output. Exit codes are stable:
`0` = ready to build, `1` = at least one failing check, `2` = configuration
could not be resolved. Every emitted string is redacted — credential-shaped
values (AKID…, `secret_key=…`, PEM private keys) never leave the process.

`plan` never creates or changes cloud resources. It previews the temporary
CVM, placement, maximum duration, release gates, distribution and cost caveat.

### Team state and cloud Canary

`state` manages the evidence directory (`OHBS_IMAGE_STATE_DIR` or
`~/.ohbs-image`): `state path` prints it, `state status` reports evidence
counts and disk usage, `state init` creates the layout idempotently (safe in
CI), and `state prune` retains recent lineage while dropping superseded
per-run evidence (runs/plans/provenance — the permanent release approval
trail in `releases/` is never pruned). Use `--dry-run` to preview.

`state sync` copies the evidence directory to/from a local team directory or
Tencent COS. The COS backend uses the official `coscli` binary and its
credential/config mechanism, so secrets are never passed on the command line.
`--check` previews transfers without copying (local backend only).
Orchestration should pull before a state-aware operation and push afterwards.
Set `OHBS_IMAGE_COSCLI_CONFIG` when CI uses a non-default coscli
configuration file.

The opt-in `.github/workflows/cloud-canary.yml` runs a weekly real-cloud
acceptance (TencentOS 3 L1 by default) only when repository variable
`OHBS_ENABLE_CLOUD_CANARY=true`. It is disabled by default because it creates
billed CVMs; manual runs require explicit cost confirmation. Manual dispatch
additionally accepts `profile` (`tencentos3` / `win2022`), `level` (1/2) and
`build_instance_type` (default `SA5.MEDIUM2`) so a canary can be targeted
without editing the workflow.

## Configuration

`ohbs-image.toml` is the single source of truth — no manual template editing.

Validation is strict about types: list options (`rules_include`,
`rules_exclude`, `share_accounts`, `share_org_units`, `test_components`) must
be TOML arrays of non-empty strings, and `level`, `min_score`, `assume_role_duration`, `ssh_port`
must be integers (floats and booleans are rejected). The hardening section is
`[ohbs]` (what `ohbs-image init` generates); the legacy `[cis]` name is still
accepted — if both exist, `[ohbs]` wins with a warning.

Configuration files can be layered for environment-specific overrides:
pass `--overlay <file>` on any command (repeatable) or use
`ohbs-image config merge base.toml team.toml local.toml` to preview the
merged result. Layers deep-merge: tables merge recursively and later files
win key-by-key; lists and scalars are replaced, not appended. Overlay files
may be partial — only the merged result must be complete and valid.

```toml
[build]
profile             = "tencentos3"
#   Linux: ubuntu2004 | ubuntu2204 | ubuntu2404 |
#          rhel8 | rhel9 | rhel10 | rocky9 |
#          tencentos3 | tencentos4
#   Windows: win2016 | win2019 | win2022 | win2025
region              = "ap-guangzhou"
zone                = "ap-guangzhou-4"
instance_type       = "S5.MEDIUM2"
source_image_id     = "img-xxxxxxxx"
vpc_id              = "vpc-xxxxxxxx"
subnet_id           = "subnet-xxxxxxxx"
security_group_id   = "sg-xxxxxxxx"
associate_public_ip = false              # use a private VPC runner or bastion for builds
# max_build_minutes = 120                # hard Packer wall-clock limit; stops stalled builds and caps cloud cost (15-1440)
# spot = true                             # use a spot instance for the build VM (up to ~90% cheaper)
# instance_name = "my-build-cvm"          # optional explicit name for the temporary build CVM ("" = plugin auto)
# # [build.packer] — passthrough of arbitrary tencentcloud-cvm Packer builder
# # args (inherits the full Packer capability set). E.g. some SA-series
# # instance types do not support the default CLOUD_PREMIUM root disk:
# #   [build.packer]
# #   disk_type = "CLOUD_SSD"
# #   disk_size = 100

[image]
name_prefix  = "tencentos3-cis"
# name = "my-ohbs-image"                  # optional: fixed image name (empty = auto prefix-level-timestamp)
copy_regions = ["ap-shanghai"]            # [] to disable cross-region copy
# share_accounts = ["uin/1234567890"]    # optional: share the built image with other accounts
# share_org_units = ["ou-xxxx"]          # NOT supported: ModifyImageSharePermission takes
                                        # account IDs only — the tool warns and skips this

[ohbs]
level = 1                                 # 1 or 2
# min_score = 85                          # post-reboot audit gate (0 disables; default 85)
# allow_disruptive = true                 # apply disruptive remediations during the build (default true)
# rules_include = ["1.5.6"]               # run only these rules
# rules_exclude = ["1.1.2.2.4"]           # always wins over rules_include
# allow_scoped_approval = true             # explicitly permit a subset-built image to be approved (default false)
# Per-control parameter overrides (deep-merged into the catalog at render):
# [ohbs.overrides."5.2.2"]
# ssh_max_auth_tries = 4

[cloud]
secret_id_env  = "TENCENTCLOUD_SECRET_ID"
secret_key_env = "TENCENTCLOUD_SECRET_KEY"
# winrm_password_env = "WINRM_PASSWORD"   # Windows only
# Group-account (organization) cross-account builds — assume a CAM role in
# the target account using the local AK/SK:
# assume_role_arn      = "qcs::cam::uin/1234567890:roleName/CrossAccountBuilder"
# assume_role_session  = "ohbsimage-build"   # optional, default "ohbs-image"
# assume_role_duration = 3600             # optional, default 7200, range 0-43200
# OIDC / STS temporary credentials (CI, no long-lived AK/SK):
# security_token_env = "TENCENTCLOUD_SECURITY_TOKEN"   # Packer default

# Build notifications (WeCom group-robot webhook). Empty webhook = off.
# [notify]
# webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxx"
# on      = "failure"        # always | success | failure
# deploy_webhook = "https://ci.example.com/api/images"  # POST image metadata on success (EventBridge-style)

# SLSA-style provenance signing (GPG). Empty = provenance unsigned.
# [sign]
# gpg_key = "ABCDEF0123456789"

[meta]
os_tag    = "tencentos-3"
benchmark = "CIS-v1.0.0"
# smoke_test = true           # instance-level checks before the image snapshot
# cve_scan   = false          # optional: trivy vulnerability gate before the snapshot
# sbom       = false          # optional: emit an SBOM into the image + provenance
# delivery_report_required = false  # optional: fail release if the HTML delivery report cannot be written
# verify_boot = false         # optional: boot a probe from the produced image and re-audit
# test_components = ["scripts/app-check.sh"]  # optional: user test scripts run before snapshot
```

### Full reference

| Section | Field | Type | Notes |
|---|---|---|---|
| `[build]` | `profile` | string | Profile name from supported list |
| | `region` | string | e.g. `ap-guangzhou` |
| | `zone` | string | e.g. `ap-guangzhou-4` |
| | `instance_type` | string | e.g. `S5.MEDIUM2` |
| | `source_image_id` | string | OS public image ID |
| | `vpc_id` | string | VPC identifier |
| | `subnet_id` | string | Subnet identifier |
| | `security_group_id` | string | Must start with `sg-` |
| | `associate_public_ip` | bool | Assign public IP |
| | `max_build_minutes` | int | Hard wall-clock limit for the complete Packer operation — plugin init retries and build/test/scan — (default `120`, range `15`–`1440`); timed-out Packer is terminated and the normal failure/cleanup path runs |
| | `spot` | bool | Use a spot instance for the build VM (`instance_charge_type=SPOTPAID`; up to ~90% cheaper, may be repossessed mid-build, default `false`) |
| | `instance_name` | string | Optional explicit name for the temporary build CVM (empty = Packer auto-generates) |
| | `packer` | table | Passthrough of arbitrary `tencentcloud-cvm` Packer builder args (e.g. `disk_type`, `disk_size`, `data_disks`), injected verbatim into the generated HCL source block |
| `[image]` | `name_prefix` | string | Output image name prefix |
| | `name` | string | Fixed image name (empty = auto `prefix-level-timestamp`) |
| | `copy_regions` | []string | Regions to replicate (empty = skip) |
| | `share_accounts` | []string | Share the built image with other accounts (`uin/…`) after build (empty = off) |
| | `share_org_units` | []string | Not supported — `ModifyImageSharePermission` accepts account IDs only; the tool warns and skips this option (use `share_accounts`) |
| `[ohbs]` | `level` | int | `1` (Level 1) or `2` (Level 2) |
| | `min_score` | int | Post-reboot audit gate (default `85`; `0` disables) |
| | `allow_disruptive` | bool | Apply disruptive remediations during the build — mount options, service removals, etc. (default `true`; the build VM is ephemeral and rebooted before the audit, so these are safe) |
| | `rules_include` | []string | Rule-ID filter — when set, ONLY these rules run (empty = all) |
| | `rules_exclude` | []string | Rule-ID filter — always wins over `rules_include` |
| | `allow_scoped_approval` | bool | Explicitly permit approval of an image built from a rule subset (default `false`) |
| | `overrides` | table | Per-control parameter overrides, keyed by rule ID — deep-merged into the catalog at render time (e.g. `[ohbs.overrides."5.2.2"]`) |
| `[cloud]` | `secret_id_env` | string | Env var for Secret ID |
| | `secret_key_env` | string | Env var for Secret Key |
| | `security_token_env` | string | STS session-token env var (default `TENCENTCLOUD_SECURITY_TOKEN`; used with OIDC/STS credentials) |
| | `winrm_password_env` | string | Windows admin password env var |
| | `assume_role_arn` | string | Group-account CAM role ARN (empty = off). e.g. `qcs::cam::uin/12345:roleName/X` |
| | `assume_role_session` | string | AssumeRole session name (default `ohbs-image`) |
| | `assume_role_duration` | int | Session seconds, 0-43200 (default 7200) |
| `[meta]` | `os_tag` | string | Tag value for output image |
| | `benchmark` | string | CIS benchmark version tag (pinned in lineage/provenance for auditability) |
| | `ssh_port` | int | SSH port (default `22`; TencentOS: `36000`) |
| | `ssh_timeout` | string | Packer SSH timeout (default `"15m"`) |
| | `ssh_debug_password` | string | Root password for VNC debug (default empty) |
| | `smoke_test` | bool | Instance-level checks before snapshot (default `true`) |
| | `cve_scan` | bool | Trivy CRITICAL-severity vulnerability gate before the snapshot (default `false`) |
| | `sbom` | bool | Emit an SBOM (`/opt/ohbs-image-SBOM.jsonl`) into the image, hash it and pin it in lineage + provenance (default `false`) |
| | `delivery_report_required` | bool | Fail release when the HTML delivery report cannot be archived (default `false`) |
| | `verify_boot` | bool | After the snapshot, boot a probe instance from the produced image, re-audit every CIS rule on fresh boot and gate (SSH for Linux; NTLM WinRM for Windows; default `false`) |
| | `test_components` | []string | User-defined test scripts run sequentially before the snapshot (Image Builder test-component style); non-zero exit aborts the build (empty = off) |
| `[notify]` | `webhook` | string | WeCom group-robot webhook URL (empty = off) |
| | `on` | string | `always` \| `success` \| `failure` (default `failure`) |
| | `deploy_webhook` | string | POST `{image_id, score, profile}` on build success to trigger downstream CI/CD (EventBridge-style; empty = off) |
| `[sign]` | `gpg_key` | string | GPG key id/fingerprint for provenance signing (empty = unsigned) |
| `[attestation]` | `required` | bool | Require a valid provenance signature before approval, sharing or deployment (defaults to `true` when `gpg_key` is set) |

---

## Architecture

<p align="center">
  <img src="docs/ohbs-image-architecture.png" alt="ohbs-image build architecture — TOML config to hardened golden image" width="720">
</p>

### Linux pipeline

Four phases executed inside the ephemeral CVM via `ansible-local`:

1. **Install** — provisions `ansible-core` via the OS package manager + pip.
2. **Harden** — runs the bundled ohbs-os engine (`ohbs_engine.py` + `rules.json`). Variables: `cis_mode: apply`, `cis_profile: L1/L2`.
3. **Reboot + re-audit** — reboots the instance and re-runs only the rules that were pending a reboot. Catches kernel parameters, audit daemon configs, and other settings that only take effect after restart.
4. **Gate** — final score check against a configurable threshold (default 85%). If the score falls below, `ansible-playbook` exits non-zero and Packer fails the build — the image is never created.

#### SSH access safety net

CIS rules can disable root SSH login (`PermitRootLogin no` — TencentOS 3 rule
5.1.22 / TencentOS 4 rule 5.2.10). Because the builder itself connects as
`root`, this would lock the build out after the reboot. ohbs-image therefore
adds two orchestration-layer guarantees that are regenerated on every build
(they can never go stale):

1. **Dedicated build user `ohbsimage`** — created by `install-ansible.sh` with
   passwordless sudo and the same `authorized_keys` as the current SSH user,
   so it can reconnect even if root login is fully disabled.
2. **SSH guard** — opens the live SSH port in firewalld / nftables /
   iptables, and if a CIS rule set `PermitRootLogin no`, temporarily restores
   key-based root login so Packer can reconnect.

The **final image ships hardened**: the cleanup provisioner re-applies
`PermitRootLogin no` before the snapshot is taken. To administer a built
image, use the `ohbsimage` user (`sudo -i` for root), or create your own user —
root password login is disabled by design per CIS.

#### What ships in the image (Linux)

Every Linux build leaves a ohbs-image paper trail inside the image so admins know
exactly what was done and which admin channel to use:

| Path | Purpose |
|------|---------|
| `/etc/ohbs-image/banner` | ASCII banner with the ohbs-image logo + image metadata (colored). |
| `/etc/motd` | The same banner + build summary, shown after SSH login. |
| `/etc/issue`, `/etc/issue.net` | Plain-text version for serial / network console. |
| `/etc/ssh/sshd_config.d/99-ohbs-image-banner.conf` | Wires the SSH `Banner` directive. |
| `/opt/ohbs-image-REPORT.md` | Full hardening report (what was done, score, follow-ups). |
| `/opt/ohbs-image-AUDIT-RESULT.json` | Raw re-audit JSON (the gate result). |
| `/usr/local/bin/ohbs-image-info` | One-shot summary command: `ohbs-image-info`. |

Windows builds ship the same audit evidence at
`C:\ProgramData\ohbs-image\AUDIT-RESULT.json` (raw engine result.json from the
build-time audit — the Windows counterpart of `/opt/ohbs-image-AUDIT-RESULT.json`).

```bash
$ ssh ohbsimage@<host>
              .---..---.
          .-'          '-.           OHBS IMAGE
        .'                '.           ___ ___  ___  ___
      .'                    '.       / __/ _ \/ __|/ __|
     /         ()    ()       \      | (_| (_) \__ \ (__ 
    |                        |       \___\___/|___/\___|
     \                      /         OHBS-HARDENED IMAGE BUILDER
      '.                  .'
        '.              .'
          '---.------.---'

Image:    t3-cis-level1-20260806-173729
Source:   img-test-abc123
OS/Level: tencentos-3 / level1-server
Built:    2026-08-06T17:37:29Z by ohbs-image 0.10.0

[ REPORT  ] cat /opt/ohbs-image-REPORT.md     (or run: ohbs-image-info)
[ ADMIN   ] ssh ohbsimage@<host>            (root login disabled per CIS 5.1.22)
[ ESCALATE] sudo -i                        (NOPASSWD via /etc/sudoers.d/ohbsimage-build)
```

The report at `/opt/ohbs-image-REPORT.md` documents what ohbs-image did to the base
image (per-rule counts, outstanding failures, how to re-run the scan) so
the next admin does not have to guess.

### Windows pipeline

Windows builds use the Packer `ansible` provisioner (controller-side) over WinRM. The bundled role includes `ohbs_engine.ps1` (PowerShell). The controller requires `ansible-core` locally.

| | Linux | Windows |
|---|---|---|
| Communicator | SSH | WinRM |
| Packer provisioner | `ansible-local` (runs in the CVM) | `ansible` (controller-side) |
| Engine | `ohbs_engine.py` | `ohbs_engine.ps1` |
| Controller requirement | none — engine runs on the instance | `ansible-core` on the build machine |
| Reboot safety net | `ohbsimage` build user + SSH guard | WinRM direct (no reboot lockout risk) |

### Design

**Bundled roles.** All 13 ohbs-os engine roles ship inside `ohbs_image/roles/`. At build time the tool copies the selected role into the workspace. No Galaxy, no network dependency, no version drift.

**ansible-local (Linux).** Playbooks and roles execute inside the build instance — the Packer controller does not need SSH access into the cloud VPC.

**ansible (Windows).** Controller-driven over WinRM. The controller must have `ansible-core` installed locally.

**Build-time gate.** The gate runs inside the Ansible role (`cis_fail_on_findings`). Configurable score threshold ensures the image is good enough to ship, or no image is produced.

**Credentials.** AK/SK via environment variables only (`sensitive = true` in HCL). Ephemeral instances are tagged and auto-recycled. Image tags record CIS level, OS, and benchmark.

---

## Profiles

### Linux (SSH × ansible-local)

| Profile | OS | SSH User | Pkg Manager | Role |
|---|---|---|---|---|
| `ubuntu2004` | Ubuntu 20.04 LTS | ubuntu | apt | `roles/cis-ubuntu2004/` |
| `ubuntu2204` | Ubuntu 22.04 LTS | ubuntu | apt | `roles/cis-ubuntu2204/` |
| `ubuntu2404` | Ubuntu 24.04 LTS | ubuntu | apt | `roles/cis-ubuntu2404/` |
| `rhel8` | RHEL 8 | root | dnf | `roles/cis-rhel8/` |
| `rhel9` | RHEL 9 | root | dnf | `roles/cis-rhel9/` |
| `rhel10` | RHEL 10 | root | dnf | `roles/cis-rhel10/` |
| `rocky9` | Rocky Linux 9 | root | dnf | `roles/cis-rocky9/` |
| `tencentos3` | TencentOS Server 3 | root | dnf | `roles/cis-tencentos3/` |
| `tencentos4` | TencentOS Server 4 | root | dnf | `roles/cis-tencentos4/` |

### Windows (WinRM × controller-side ansible)

| Profile | OS | User | Role |
|---|---|---|---|
| `win2016` | Windows Server 2016 | Administrator | `roles/cis-win2016/` |
| `win2019` | Windows Server 2019 | Administrator | `roles/cis-win2019/` |
| `win2022` | Windows Server 2022 | Administrator | `roles/cis-win2022/` |
| `win2025` | Windows Server 2025 | Administrator | `roles/cis-win2025/` |

To switch profiles, change `[build].profile` and `source_image_id` in `ohbs-image.toml`.

---

## Test Matrix

Validated CIS-hardened images across the supported OS × level grid. All builds
ran on Tencent Cloud Guangzhou region with `cis_allow_disruptive: false`;
every image below was re-verified in the console as `NORMAL` on 2026-08-14.

Full matrix — including the CVM instance type used per build —
is in **[tests/TEST-MATRIX.md](tests/TEST-MATRIX.md)**.

---

## CI/CD Integration

```bash
export TENCENTCLOUD_SECRET_ID=xxx
export TENCENTCLOUD_SECRET_KEY=xxx
ohbs-image build --log-file build.log
```

Point downstream CVM / Auto Scaling / Terraform at the output `image_id`. Pin the build machine to a dedicated VPC and security group.

---

## Security model (for enterprise review)

What auditors usually ask about, and where each control lives:

- **No long-lived credentials for humans.** The person (or pipeline)
  triggering a build holds only cloud API permissions — OIDC/STS
  short-lived credentials in CI, a least-privilege sub-account, or an
  `assume_role` chain for group accounts. Nobody needs a VM password or
  SSH key to run a build. See [Group accounts](#group-accounts-organization).
- **Ephemeral, isolated build VM.** The build instance lives for the
  duration of one build (~10 min), sits in a dedicated VPC with a
  security group source-restricted to the build machine's egress IP,
  uses a Packer-generated throwaway keypair that is deleted when the
  build ends, and is terminated automatically — success or failure.
- **The build VM runs as root — deliberately.** This follows AWS EC2
  Image Builder / Azure Image Builder / Packer's own examples, where the
  ephemeral build instance is driven as root (or a NOPASSWD-sudo default
  user, which is the same privilege set under another name). There is no
  production data, no multi-user access, and no persistence on this VM;
  least-privilege controls apply to *who can trigger the pipeline*, not
  to a throwaway VM nobody can log into. The shipped artifact is what
  matters — and it is hardened: root SSH login is disabled
  (`PermitRootLogin no`) before the snapshot, per CIS.
- **Auditable output.** Every build records lineage (source image →
  output image IDs, score, version), can emit a GPG-signed SLSA-style
  provenance statement, an SBOM pinned into the provenance, and
  SARIF/XCCDF reports for GRC ingestion. The image itself carries the
  audit result (`/opt/ohbs-image-AUDIT-RESULT.json`) and a full report
  (`/opt/ohbs-image-REPORT.md`).

---

## Group accounts (organization)

ohbs-image supports the Tencent Cloud group-account (企业组织) pattern for
**cross-account golden image builds** — build once from a central account,
distribute everywhere:

- **Build as a target account**: set `[cloud].assume_role_arn` to a CAM
  role created in the target account. Packer assumes that role with the
  local AK/SK (STS `AssumeRole`), so the instance and image are created
  *in the target account* while credentials stay in the central account.

  ```toml
  [cloud]
  assume_role_arn      = "qcs::cam::uin/1234567890:roleName/CrossAccountBuilder"
  assume_role_session  = "ohbsimage-build"   # optional
  assume_role_duration = 3600             # optional, default 7200
  ```

  The role needs the same CAM permissions the builder requires
  (`cvm:RunInstances`, `cvm:CreateImage`, `cvm:DescribeImages`) plus a
  trust policy allowing the central account to assume it.

- **Build then share**: keep `assume_role_arn` empty, build in the central
  account, and share the resulting image to business accounts via the
  Tencent Cloud console or the `image_share_accounts` Packer option.

When `assume_role_arn` is empty (the default) builds behave exactly as
before — no group-account setup required.

### OIDC / STS credentials (no long-lived AK/SK)

For CI pipelines (GitHub Actions etc.) you can build **without storing any
AK/SK**: the runner obtains short-lived STS credentials via OIDC federation,
and ohbs-image hands the session token straight to Packer.

1. **CAM side (one-time)**: create an OIDC identity provider pointing at
   `https://token.actions.githubusercontent.com`, then create a CAM role
   whose trust conditions pin `oidc:iss`, `oidc:aud` (the client ID you
   configured) and `oidc:sub` (e.g. `repo:susunola/ohbs-image:
   ref:refs/heads/main`). Attach the builder permissions
   (`cvm:RunInstances`, `cvm:CreateImage`, `cvm:DescribeImages`, ...).

2. **Workflow**: exchange the OIDC token for STS credentials with
   `everpcpc/tencentcloud-oidc-auth@v1`, which exports
   `TENCENTCLOUD_SECRET_ID`, `TENCENTCLOUD_SECRET_KEY` and
   `TENCENTCLOUD_SECURITY_TOKEN` — Packer reads all three natively:

   ```yaml
   permissions:
     id-token: write          # required for OIDC
   steps:
     - uses: everpcpc/tencentcloud-oidc-auth@v1
       with:
         role-arn: qcs::cam::uin/1234567890:roleName/ci-builder
         oidc-provider-id: github
         region: ap-guangzhou
     - run: ohbs-image build --config ohbs-image.toml
   ```

   Run the workflow from a self-hosted runner with private connectivity to the
   build VPC. Keep `associate_public_ip = false` and restrict the build
   security group to the runner or bastion security group; do not open SSH or
   WinRM to the public internet for a golden-image build.

3. **ohbs-image side**: nothing to configure — the default
   `security_token_env = "TENCENTCLOUD_SECURITY_TOKEN"` is picked up
   automatically. Override it only if your CI exports the token under a
   different name:

   ```toml
   [cloud]
   security_token_env = "MY_CI_STS_TOKEN"
   ```

Note: `security_token` and `assume_role` are independent — STS credentials
can themselves be scoped to the OIDC role, so you typically do not need
both at once.

### Build → test → distribute (image governance)

Beyond building the image, ohbs-image covers the governance loop that Packer
itself leaves to you (mirroring AWS Image Builder's build → test →
distribute pipeline):

- **Test (before snapshot)** — after finalize + re-audit, an instance-level
  smoke test runs on the live VM *before* Packer snapshots it: `sshd -T`
  parses, sshd/auditd active, `/dev/shm` carries `noexec`, no weak SSH
  crypto, journal-upload active (when configured). Any failure aborts the
  build — **no image is produced**. Disable with `[meta].smoke_test = false`.

- **Lineage (distribute metadata)** — every build appends a record
  (`~/.ohbs-image/lineage.jsonl`): source image → output image IDs, level,
  region, score, version, timestamp, and a `mode` field (`build` / `scan` /
  `test`). Scan and test images are recorded so `cleanup images` can retire
  them, but they are not hardened builds — `--skip-if-unchanged` and
  `pending` ignore them (only `mode: build` records count; records written
  before the field existed count as `build`).  The full per-rule audit JSON is
  archived alongside it on the build machine at
  `~/.ohbs-image/reports/<image-name>.<run-id>.json`.  Query it with:

  ```bash
  ohbs-image images            # recent builds, newest first
  ohbs-image images --latest   # the most recent record
  ohbs-image cleanup images --older-than 30   # dry-run: what would be retired
  ohbs-image cleanup images --older-than 30 --apply   # actually delete
  ```

  `cleanup images` retires golden images older than N days (default 30),
  always keeping the newest build (`--keep-latest`, default 1). It uses the
  lineage records to find the image IDs, verifies them via
  `cvm:DescribeImages`, deletes via `cvm:DeleteImages` (stdlib TC3-signed —
  no extra dependencies), and marks the lineage entries `retired`. Credentials
  come from `TENCENTCLOUD_SECRET_ID` / `TENCENTCLOUD_SECRET_KEY`
  (optionally `TENCENTCLOUD_SECURITY_TOKEN`). Pair with cron/systemd timer
  for fully automatic retirement.

- **Notify (scheduling companion)** — post build results to a WeCom group
  robot. Combine with cron / systemd timer / SCF for scheduled rebuilds:

  ```toml
  [notify]
  webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxx"
  on      = "failure"     # always | success | failure
  ```

  ```bash
  # systemd timer / cron example — rebuild monthly, only notify on failure
  0 3 1 * *  ohbs-image build --config /etc/ohbs-image/ohbs-image.toml -y
  ```

- **SLSA-style provenance** — after a successful build ohbs-image writes a
  signed provenance statement (`~/.ohbs-image/provenance/…provenance.json`)
  describing exactly what produced the image (source image, profile, level,
  region, ohbs-image version, score). Tencent CVM images are `img-*` artifacts,
  not OCI images, so cosign container signing does not apply — instead the
  provenance file is GPG-detached-signed (`[sign].gpg_key`), giving an
  auditable, tamper-evident record (SLSA L1 + signed provenance). Verified
  end-to-end with a real GPG key — tampering with the provenance makes
  verification fail (`gpg: BAD signature`).

  ```toml
  [sign]
  gpg_key = "ABCDEF0123456789"   # your GPG key id/fingerprint

  [attestation]
  required = true                  # fail closed: never distribute an unsigned build
  ```

  Verify any signed provenance (audit / compliance):

  ```bash
  ohbs-image verify provenance --provenance ~/.ohbs-image/provenance/xxx.provenance.json
  ohbs-image verify provenance --image img-ekny61ig        # auto-locate by image ID
  ohbs-image verify provenance --provenance ~/.ohbs-image/provenance/xxx.provenance.json \
    --trusted-key-fingerprint ABCDEF0123456789ABCDEF0123456789ABCDEF01
  ```

  Output shows subject (image IDs), profile/level/region/source, builder
  version, re-audit score, and the GPG signature status (VALID / INVALID /
  NONE). `--trusted-key-fingerprint` may be repeated to form a signer
  allowlist. Exit code is non-zero when the signature is missing, invalid,
  cannot be checked because GPG is unavailable or times out, the signer is not
  allowlisted, or `--image` is not a subject of the provenance.

  With `[attestation].required = true`, a missing/failed/timed-out signature
  blocks approval, cross-account sharing, and the deploy webhook. For CI/CD,
  `ohbs-image build --result-file result.json` writes one atomic JSON document
  containing the run ID, image IDs, score, evidence paths, and signature state.
  Evidence is stored with owner-only permissions under `~/.ohbs-image`; each
  build receives a UUID run ID, so concurrent builds do not reuse provenance
  filenames or invocation IDs.

  Webhook endpoints must use HTTPS and cannot use literal non-public IP
  addresses. DNS webhook hosts remain administrator-controlled integrations;
  production networks should also enforce an egress allowlist. Deploy events carry the build `run_id` as
  both `event_id` and `Idempotency-Key`; delivery retries transient failures
  three times. Use `--state-dir /secure/ohbs-state` (or
  `OHBS_IMAGE_STATE_DIR`) to isolate CI jobs or retain team evidence outside a
  runner home directory.

  WeCom notifications are intentionally human-facing, best-effort messages:
  they do not gate release and are not a delivery receipt. Automation must use
  the result JSON and/or deploy webhook, both correlated by `run_id`.

  Every build and clean-boot probe also writes a versioned run manifest under
  `runs/<run_id>.json`. It records the active lease, phase, and known temporary
  resources. Long-running Packer builds refresh that lease every five minutes.
  `cleanup runs` skips unexpired active leases; it only treats age as a fallback
  for expired or pre-manifest resources.

  `[build.packer]` remains a privileged provider-extension escape hatch. Its
  values are not copied into provenance, but the exact sorted key set and a
  SHA-256 digest of the complete override map are recorded for audit.

  An approved image also receives a self-contained HTML delivery report beside
  the archived audit JSON in `reports/`. It summarizes release status, score,
  image IDs, run identity and evidence paths; when `--result-file` is used,
  its path is returned as `html_report` for CI links and artifacts.

  Every approved image also receives a release manifest under
  `releases/<image-id>.json`. It binds the image to hashes of its audit,
  provenance and HTML evidence, then records a cloud-agnostic promotion trail.
  `ohbs-image promote --image img-xxx --environment staging` and
  `ohbs-image rollback --image img-xxx --environment staging` only update this
  auditable release state; application deployment and cloud sharing remain
  explicit external pipeline actions.

  Before a downstream pipeline consumes an image, run
  `ohbs-image verify release --image img-xxx` against the downloaded evidence
  state. It verifies that each referenced audit, provenance, and HTML report
  remains under the state root and matches its recorded SHA-256.

### Real cloud acceptance and cleanup drills

Three protected GitHub workflows turn the operational checks into repeatable,
reviewable runs without creating surprise cloud spend:

- `real-cloud-acceptance` requires the protected `cloud-e2e` environment and
  an explicit cost confirmation. It runs exactly one profile/level through a
  real build and clean-boot verification. Configure
  `TC_E2E_OIDC_ROLE_ARN`, `TC_E2E_REGION`, `TC_E2E_ZONE`, `TC_E2E_VPC_ID`,
  `TC_E2E_SUBNET_ID`, `TC_E2E_SG_ID`, and `TC_E2E_SOURCE_IMAGE_ID` as
  environment secrets. Windows also needs `TC_E2E_WINRM_PASSWORD`.
- `ephemeral-cleanup-drill` is dry-run by default. Its `apply` input is the
  only path that terminates tagged ephemeral CVMs; protect the same environment
  with reviewers and inspect the dry-run artifact first.
- `cloud-canary` adds a weekly TencentOS 3 L1 acceptance, but its scheduled
  job remains inert until repository variable
  `OHBS_ENABLE_CLOUD_CANARY=true`. Manual runs require cost confirmation and
  may select TencentOS 3 or Windows Server 2022. Configure
  `TC_CANARY_TENCENTOS3_IMAGE_ID` and `TC_CANARY_WIN2022_IMAGE_ID` in the same
  protected environment.

All workflows use short-lived OIDC credentials and keep their logs/evidence
as 90-day artifacts. They are intentionally manual until the organization has
accepted the associated cloud cost and change-control policy.

- **SBOM + change detection (supply chain)** — with `[meta].sbom = true` the
  build emits a zero-dependency SBOM (`/opt/ohbs-image-SBOM.jsonl`, native
  rpm/dpkg query) into the image, and its SHA-256 + package count are pinned
  in lineage and the provenance statement (`sbomSha256` /
  `sbomPackageCount`) — SLSA L2-style evidence of what exactly shipped.
  `ohbs-image build --skip-if-unchanged` / `ohbs-image pending` compare a
  deterministic input fingerprint (source image, rule catalog hash,
  benchmark, level, filters) against the last successful `mode: build`
  lineage record and skip the rebuild when nothing changed — a
  scheduled-pipeline cost saver.

  ```bash
  ohbs-image build --skip-if-unchanged    # skip if inputs unchanged
  ohbs-image pending                      # exit 0 = no rebuild needed, 1 = rebuild
  ```

- **Clean-boot verification (`verify image`)** — AWS Image Builder runs its
  test phase on the *output* image, not the build instance. `ohbs-image
  verify image --image img-xxx` boots a probe instance from the produced
  image, runs the bundled engine in scan mode on the FRESH boot (catching
  SELinux relabel stalls, first-boot services, cloud-init reconfiguration),
  gates on the score, and always terminates the probe. The probe uses a
  throwaway ed25519 key pair (created locally, imported via
  `cvm:ImportKeyPair`, deleted afterwards) and logs in as the image's
  built-in `ohbsimage` user — root login is disabled by the hardening, so no
  credentials need to be supplied. Windows probes instead set a new,
  per-probe Administrator password through `RunInstances`, authenticate with
  NTLM WinRM, and run the exact engine/catalog retained in the image; the
  password is never logged or persisted. `[meta].verify_boot = true` chains
  either path automatically after every successful build.

  ```bash
  ohbs-image verify image --image img-ekny61ig --min-score 85
  ```

- **Independent audit (`audit`)** — the score is no longer only self-
  reported by the engine that applied the hardening. `ohbs-image audit` runs a
  third-party tool and gates on the result, exactly like dev-sec (InSpec) /
  RHEL (oscap + SCAP content) / ansible-lockdown (Goss):

  ```bash
  # OpenSCAP — RHEL-family: use the scap-security-guide datastream on target
  ohbs-image audit --tool oscap --host 1.2.3.4 --ssh-user root \
    --datastream /usr/share/xml/scap/ssg/content/ssg-rhel9-ds.xml \
    --profile xccdf_org.ssgproject.content_profile_cis --min-score 85

  # Chef InSpec — dev-sec baselines (Linux)
  ohbs-image audit --tool inspec --host 1.2.3.4 --ssh-user root \
    --baseline dev-sec/linux-baseline --min-score 85

  # HardeningKitty — Windows cross-check (audit runs on the Windows host,
  # export the CSV, parse it here)
  ohbs-image audit --tool kitty --parse kitty-audit.csv --min-score 85
  ```

  Every audit can emit SARIF / XCCDF for GRC ingestion
  (`--sarif out.sarif --xccdf out.xml`).

### Post-delivery lifecycle (drift / refresh / deploy trigger)

- **Drift detection** — an image is correct at build time, but instances
  launched from it drift (configs tweaked, packages patched, services
  changed). `ohbs-image drift` re-scans a LIVE instance over SSH and diffs the
  result against the baseline — the audit result shipped inside the image
  (`/opt/ohbs-image-AUDIT-RESULT.json`) or a saved one:

  ```bash
  ohbs-image drift --host 1.2.3.4 --image img-ekny61ig --min-score 85
  # reports: new failing rules / recovered rules / score delta; exit 1 = drift
  ohbs-image drift --host 1.2.3.4 --save-baseline   # persist a custom baseline
  ```

- **Vendor image refresh** — when the upstream OS image is updated, the
  golden image should be rebuilt. `ohbs-image check-source` compares the
  source image's `CreatedTime` against the last build's lineage record
  (exit 0 = unchanged, 1 = refreshed, 2 = API state unknown); schedule it
  on a timer ahead of `build --skip-if-unchanged`. Treat exit 2 as a failed
  check rather than silently skipping a rebuild:

  ```bash
  ohbs-image check-source && echo "source unchanged" || ohbs-image build -y
  ```

- **Deploy trigger** — `[notify].deploy_webhook` POSTs
  `{event: "image.ready", image_id, score, profile, region}` on build
  success, so a new image automatically drives the downstream release
  (ASG launch-template update, Terraform, CI pipeline) instead of waiting
  for a human to read the WeCom message.

- **Cost control** — `[build].spot = true` launches the ephemeral build VM
  as a spot (竞价) instance (`instance_charge_type=SPOTPAID`, up to ~90%
  cheaper); repossess risk is acceptable for a short-lived build machine.

- **Safe cleanup** — `cleanup images --unused-since N` only deletes images
  that are NOT shared with other accounts (via
  `DescribeImageSharePermission`), so an image still referenced downstream
  is not retired while it is fresh. The guard expires N days after the
  image's lineage record: a shared image whose record is older than N days
  (undated records count as ancient) is presumed unused since then and is
  retired anyway; `0` disables the guard entirely. Fails open (keeps the
  image) on API errors.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `preflight` fails with credential error | AK/SK not exported | `export TENCENTCLOUD_SECRET_ID=...` |
| `validate` fails with plugin download | `packer init` failed (offline?) | Run with internet access — Packer caches plugins after first download |
| Packer times out waiting for SSH | SG doesn't allow TCP/22 from build IP | Add inbound rule for your egress IP — `preflight` now checks this and warns proactively when it can resolve the SG rules and your public IP |
| `ansible-playbook` can't find python3 | Source image has no Python | Python 3.6+ must be pre-installed |
| Windows build WinRM error | Password not set or TCP/5986 blocked | Export `WINRM_PASSWORD` + open inbound rule |
| Build passes but score below 85% | Gate threshold too strict for this OS | Adjust `cis_min_score` in the role, or use Level 1 |
| TencentOS 4 apply fails: `Module result deserialization failed` + missing `/tmp/ansible_...payload.zip` | ansible-core ≥ 2.16 (modular ansiballz) caches module payloads in `/tmp`, which TencentOS 4 sweeps / backs with tmpfs; the reused payload vanishes mid-run | Fixed in v0.14.4 — the venv wrapper exports `TMPDIR=/opt/ohbs-image-ansible/tmp` so payloads live on stable root-disk storage |
| TencentOS 4 reboot → Packer reconnect `i/o timeout` for 5+ min | ssh-guard runs *before* apply; CIS firewall rules (3.4.x) reload firewalld / switch the active zone, and the new zone has no SSH allow rule → port 22 is DROPped after reboot | Fixed in v0.14.8 — ssh-guard is re-run right before the reboot provisioner, and rules are persisted (`nft list ruleset > /etc/sysconfig/nftables.conf`, `iptables-save > /etc/sysconfig/iptables`) |
| `packer build` fails at prepare: `Unsupported argument "ansible_env_vars"` | `ansible_env_vars` only exists on the `ansible` (non-local) provisioner, not `ansible-local` | Fixed in v0.14.4 — TMPDIR is injected via the ansible-playbook venv wrapper instead of an HCL argument |
| `packer build` fails at parse: `Missing item separator` in `main.pkr.hcl` | A missing comma between `inline = [...]` items — Python silently concatenates the two adjacent strings, HCL then sees one unterminated item | Fixed in v0.14.14 — comma restored; regression test scans every rendered inline list for missing separators |
| TencentOS 4 reboot → `i/o timeout` even with all-zone firewall rules | `/.autorelabel` left by the SELinux-disabled boot; once `SELINUX=permissive` is written the next boot runs a full early-boot relabel (before sshd) — a multi-minute-to-infinite stall | Fixed in v0.14.17 — the guard deletes the stale `/.autorelabel` before reboot (permissive needs no relabel; the mark service only recreates it during a disabled boot) |
| Post-reboot `scp: /opt/...: Read-only file system` (then `/root/...`) | TencentOS 4 ships ro entries in fstab; first SELinux enable also makes `systemd-remount-fs` fail, leaving the whole root fs ro while sshd still comes up | Fixed in v0.14.18/19 — guard strips `ro` from `/opt` and `/` fstab lines + remounts rw; the boot oneshot force-remounts `/` before sshd; post-reboot uploads moved to `/root` |
| Smoke test `SMOKE FAIL: auditd / /dev/shm / weak SSH crypto` on L1 | Assertions gated on file/unit *existence* (TOS4 ships many units) or a hand-written "weak" blacklist that contradicts CIS 1.6.5/1.6.6 (hmac-sha1/umac-64/chacha20/aes\*-cbc are allowed) | Fixed in v0.14.20-22 — assertions now gate on `is-enabled` / fstab-applied; crypto check only flags CIS-forbidden algorithms (md5/3des/rc4/blowfish/cast/salsa20) |

---

## Roadmap

- [x] CI pipeline (GitHub Actions + OIDC, zero long-lived AK/SK)
- [x] Image governance loop: smoke test / lineage / notifications / SLSA signing
- [x] `ohbs-image list` — enumerate available profiles with metadata
- [x] `ohbs-image scan` — audit-only mode (no remediation, gate on findings)
- [x] Custom rule selection (`rules_include` / `rules_exclude` in `ohbs-image.toml`)
- [x] PyPI package (`pip install ohbs-image`) — publish workflow included
- [x] Automatic image cleanup (retire old images by lineage age)
- [x] Independent audit tool (`ohbs-image audit` — oscap / inspec / kitty)
- [x] Benchmark-pinned rule IDs in engine output + SARIF (CIS-CAT cross-reference)
- [x] Clean-boot verification (`ohbs-image verify image` / `[meta].verify_boot`)
- [x] Per-control overrides (`[ohbs].overrides` in `ohbs-image.toml`)
- [x] CVE scan gate + SBOM emission (`[meta].cve_scan` / `[meta].sbom`)
- [x] Change detection (`ohbs-image pending` / `build --skip-if-unchanged`)
- [x] XCCDF 1.2 report export (`scan --xccdf`, audit `--xccdf`)
- [x] Cross-account image sharing (`[image].share_accounts`)
- [x] SBOM pinning in provenance + lineage (SLSA L2-style evidence)
- [x] Windows cross-check via HardeningKitty CSV (`audit --tool kitty`)
- [x] Config drift detection (`ohbs-image drift` vs the image baseline)
- [x] User test components (`[meta].test_components`, Image Builder style)
- [x] Deploy trigger webhook (`[notify].deploy_webhook`, EventBridge style)
- [x] Spot-instance build VM (`[build].spot`, up to ~90% cheaper)
- [x] Safe cleanup (`cleanup images --unused-since`, shared images kept within the guard window)
- [x] Sharing guard (`[image].share_org_units` is rejected with a warning — the API accepts account IDs only; use `share_accounts`)
- [x] Rule-set versioning (`ohbs-image list --versions`)
- [x] Vendor image refresh detection (`ohbs-image check-source`)
- [x] Config tooling (`config validate` / `diff` / `get` / `explain` / `migrate` / `schema`)
- [x] Layered configuration (`config merge` + repeatable `--overlay` deep-merge; tables recurse, lists/scalars replace)
- [x] Report evidence index (`report list` / `report show` / `report diff` lineage trail)
- [x] Self-contained HTML compliance report (`report html RUN_ID` re-render from evidence, `scan --html`)
- [x] Zero-cost demo (`ohbs-image try` — offline engine + catalog gates, deterministic sample audit, real HTML report; also Docker `--target try`)
- [x] Build cost tracking (`report cost` — instance type / spot / build duration facts in lineage; optional spend estimate, no billing API)
- [x] State management (`state path` / `status` / `init` / `prune`, `sync --check`)
- [x] Engine tooling (`engine list` / `engine verify` — syntax + SHA-256 drift gate)
- [x] Catalog tooling (`catalog list` / `catalog verify` — supply-chain integrity gate)
- [x] CLI supply-chain gates in CI (`engine verify` + `catalog verify` on real bundled data)
- [x] Pre-PR gate checklist synced with CI (CONTRIBUTING runs the full nine-gate sequence)
- [x] SLSA L2: reproducible release builds (pinned build environment — fixed runner image, exact Python patch, pinned build backend, `--no-isolation`)
- [ ] 等保 2.0 (MLPS) mapping — CIS rules ↔ 等保 2.0 安全通用要求 control map (海外 CIS / 国内等保 dual view: coverage map, gap analysis, 等保 section in the HTML report)
- [ ] STIG benchmark profiles (same engine, DISA content — deferred; lower priority than the 等保 2.0 mapping)

## Contributing

Bug reports and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md)
for the development setup, lint/type-check/test commands, the project's hard
constraints (zero third-party runtime dependencies, no long-lived
credentials), and the guide for adding a new CIS profile.

### Keep the docs in sync with the CLI

CI enforces that README.md always documents every subcommand and OS profile,
and that the translated quick-start READMEs (zh-CN / ja / th) never reference
a command that no longer exists (`.github/workflows/ci.yml` →
`scripts/check_readme.py`). When you add, remove, or rename a `ohbs-image`
subcommand or a profile, update the relevant sections, then verify locally
before pushing:

```bash
python3 scripts/check_readme.py --check-tests --check-translations
# exit 0 = docs current, 1 = missing items / stale translation references
```

Translations are deliberately abridged quick-starts, so the translation guard
only flags *backticked* `ohbs-image <cmd>` references that do not exist in
the CLI — it does not require every command to appear in every language.
The script reports exactly which subcommands/profiles README.md is missing,
so you can fix the docs in one pass rather than watching CI fail.

#### Validate in a clean Docker environment

To avoid depending on your local Python state, you can also run the same check
in an isolated container (installs ohbs-image from a freshly built wheel):

```bash
# Build the image; the build itself runs check_readme.py, so it succeeds only
# if README.md is current.
docker build -t ohbs-image:check-readme .

# Re-check a modified checkout without rebuilding:
docker run --rm -v "$(pwd):/app" ohbs-image:check-readme
```

The container exit code matches the script: `0` = docs current, `1` = missing
items (the missing subcommands/profiles are printed to stderr).

To evaluate the *deliverable* itself — no cloud account, no spend — the same
image also ships a `try` stage that runs the offline demo and writes the
sample report into a bind-mounted directory:

```bash
docker build --target try -t ohbs-image:try .
docker run --rm -v "$(pwd)/out:/demo/out" ohbs-image:try
# → ./out/demo-report.html + demo-audit.json + ohbs-image.toml
```

---

## CIS Benchmarks Disclaimer

**Independent project** — ohbs-image is not affiliated with, sponsored by, or endorsed by the Center for Internet Security (CIS).

This tool applies hardening rules from CIS Benchmark recommendations. CIS Benchmarks are developed and maintained by the [Center for Internet Security](https://www.cisecurity.org/) (CIS). The ohbs-os engine roles bundled in this repository are derived from [susunola/ohbs-os](https://github.com/susunola/ohbs-os) and are provided under their respective licenses.

**Running CIS hardening in `apply` mode modifies system configuration and may affect application compatibility.** Always test hardened images in a staging environment before production use. Neither the CIS organization nor the authors of this tool guarantee complete compliance — official audit requires independent assessment using CIS-CAT or equivalent tools.

---

## License

MIT — see [LICENSE](LICENSE).
