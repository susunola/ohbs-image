# Security Policy

ohbs-image is a security tooling project: it builds CIS-hardened golden images
and ships SLSA-style provenance for the artifacts it produces. We take
reports about vulnerabilities in the tool itself, the bundled ohbs-os engine,
and the released artifacts seriously.

## Supported versions

| Version | Supported |
|---|---|
| latest release (PyPI / GitHub) | ✅ |
| previous minor release | ✅ (until the next minor is out) |
| older releases | ❌ — upgrade |

We do not backport fixes to old versions; the project moves fast and the
surface area (one wheel, bundled roles, stdlib-only runtime) is small enough
that upgrading is cheap.

## Reporting a vulnerability

**Do not open a public GitHub issue for security problems.** Instead:

1. Use GitHub's private disclosure: go to
   <https://github.com/susunola/ohbs-image/security/advisories/new>
   ("Report a vulnerability" → draft a private security advisory).
2. If you cannot use the advisory form, email the maintainer directly
   (atomoswang@qq.com) with the subject prefix `[ohbs-image-security]`.

Please include, when available:

- the affected version(s) — `ohbs-image --version`
- the exact commands/config used to trigger the issue (minimal reproduction)
- impact assessment: what an attacker could do, and under which conditions
- for code issues: a patch or a clear description of the vulnerable code path

We aim to acknowledge within **48 hours** and to ship a fix in the next
release (or a dedicated patch release for high-severity issues).

## Scope

In scope:

- the `ohbs_image/` Python package (stdlib-only runtime, config parsing,
  HCL rendering, Tencent Cloud API signing, provenance/report generation)
- the bundled ohbs-os engine payloads under `ohbs_image/roles/`
- the GitHub Actions workflows that build, test and publish the project
- release artifacts on PyPI (wheel/sdist) and the provenance attached to them

Out of scope (please report upstream):

- the CIS Benchmarks content itself — see
  [Center for Internet Security](https://www.cisecurity.org/)
- the [ohbs-os](https://github.com/susunola/ohbs-os) upstream engine repo,
  if the issue is only reproducible there
- third-party tools we invoke (Packer, ansible-core, OpenSCAP, Chef InSpec,
  HardeningKitty, trivy)

## Safe harbor

We will not pursue legal action against researchers who report through the
channels above and act in good faith: no destruction of data, no disruption
of service, no exfiltration beyond what is needed to demonstrate the issue.

## Verification of release artifacts

Every release can be verified for integrity:

```bash
# SLSA-style provenance for a produced image (not for the PyPI package):
ohbs-image verify --provenance <file>
```

For the PyPI package itself, GitHub's `attest-build-provenance` attaches a
build attestation to each release; you can verify it with the `gh` CLI:

```bash
gh attestation verify dist/ohbs_image-*.whl --owner susunola
```

## Security practices (what we do)

- Zero third-party runtime dependencies (stdlib only) — a deliberately small
  supply-chain surface for the installed CLI.
- All CI action dependencies pinned to full commit SHAs.
- Secrets only via environment variables / short-lived OIDC tokens — never in
  config files, never committed.
- SLSA-style provenance and lineage recorded for every produced image.
- pip-audit + CodeQL + dependabot run in CI to catch dependency issues
  (the small dev/CI dependency set) automatically.
