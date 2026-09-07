<p align="center">
  <img src="docs/ohbs-image-logo.png" alt="ohbs-image" width="320">
</p>

<p align="center">
  <b>English</b> · <a href="README.zh-CN.md">简体中文</a> · <a href="README.ja.md">日本語</a> · <a href="README.th.md">ภาษาไทย</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/version-0.22.0-blue" alt="Version 0.22.0">
  <img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/profiles-14-orange" alt="14 profiles">
  <a href="https://github.com/susunola/ohbs-image/actions/workflows/ci.yml"><img src="https://github.com/susunola/ohbs-image/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
</p>

# ohbs-image

Build CIS-hardened Linux and Windows images on Tencent Cloud from a TOML configuration.

ohbs-image launches a temporary instance, applies bundled hardening rules, audits the result, and creates a custom image. Choose Packer or the native Tencent Cloud Linux engine, with rule-level reports and configurable quality gates.

## Install

Requires Python 3.11+. Install from this checkout to use the current native engine:

```bash
git clone https://github.com/susunola/ohbs-image.git
cd ohbs-image
pip install .
ohbs-image guide
```

Native Linux builds require OpenSSH (`ssh`, `scp`, `ssh-keygen`). Packer builds require Packer 1.12+; Windows also requires Ansible, `ansible.windows`, and `pywinrm`. See [prerequisites](README.reference.md#prerequisites).

## Quick start

Try the offline demo without a cloud account:

```bash
ohbs-image try
```

For a real build, provide Tencent Cloud credentials through environment variables, then configure your source image and network:

```bash
export TENCENTCLOUD_SECRET_ID="<secret-id>"
export TENCENTCLOUD_SECRET_KEY="<secret-key>"

ohbs-image configure
ohbs-image doctor
ohbs-image plan
ohbs-image build --builder native   # Tencent Cloud Linux
# ohbs-image build --builder packer # Linux or Windows
```

Cloud builds incur charges. Use a stable runner IP and source-restricted SSH/WinRM access. Review the selected OS, source image, and CIS benchmark before building. Windows builds also require a strong `WINRM_PASSWORD` in the process environment.

## Supported systems

14 profiles, with L1/L2 configuration. Availability does not imply every rule or image has passed validation.

| System | Profiles |
|---|---|
| Ubuntu 20.04 / 22.04 / 24.04 | `ubuntu2004`, `ubuntu2204`, `ubuntu2404` |
| RHEL 8 / 9 / 10 | `rhel8`, `rhel9`, `rhel10` |
| Rocky Linux 9 / 10 | `rocky9`, `rocky10` |
| TencentOS 3 / 4 | `tencentos3`, `tencentos4` |
| Windows Server 2016 / 2019 / 2022 / 2025 | `win2016`, `win2019`, `win2022`, `win2025` |

Build status, audit pass rate, rule coverage, and clean-boot validation are separate results. Missing evidence and manual checks must be reviewed; a score is not CIS certification.

## Documentation

- [Full reference](README.reference.md) — commands, configuration, architecture, and CI/CD.
- [Test matrix](tests/TEST-MATRIX.md) — source images and validation scope.
- [Rule quality](docs/cis-rule-quality.md) · [Evidence index](docs/public-evidence-index.md).
- [Troubleshooting](docs/troubleshooting-support.md) · [Security model](README.reference.md#security-model-for-enterprise-review).
- [Changelog](CHANGELOG.md) · [License](LICENSE).

<details>
<summary>All CLI commands</summary>

Use `ohbs-image <command> --help` for options. See the [command reference](README.reference.md#commands) for examples.

| Purpose | Commands |
|---|---|
| Get started | `ohbs-image guide`, `ohbs-image init`, `ohbs-image configure`, `ohbs-image quickstart`, `ohbs-image try` |
| Configure and build | `ohbs-image config`, `ohbs-image doctor`, `ohbs-image discover`, `ohbs-image preflight`, `ohbs-image plan`, `ohbs-image validate`, `ohbs-image build`, `ohbs-image launch`, `ohbs-image native` |
| Audit and evidence | `ohbs-image audit`, `ohbs-image scan`, `ohbs-image test`, `ohbs-image baseline`, `ohbs-image benchmark`, `ohbs-image report`, `ohbs-image proof`, `ohbs-image compliance`, `ohbs-image drift`, `ohbs-image check-source` |
| Verify | `ohbs-image verify`, `ohbs-image verify-image`, `ohbs-image verify-release` |
| Manage images | `ohbs-image images`, `ohbs-image list`, `ohbs-image pending`, `ohbs-image registry`, `ohbs-image ancestry`, `ohbs-image channel`, `ohbs-image promote`, `ohbs-image rollback`, `ohbs-image consumer`, `ohbs-image distribution` |
| Operate | `ohbs-image state`, `ohbs-image run`, `ohbs-image event`, `ohbs-image worker`, `ohbs-image serve`, `ohbs-image policy`, `ohbs-image cve`, `ohbs-image dr`, `ohbs-image upgrade` |
| Extend | `ohbs-image provider`, `ohbs-image extension`, `ohbs-image engine`, `ohbs-image catalog` |
| Clean up | `ohbs-image clean`, `ohbs-image cleanup`, `ohbs-image cleanup-images`, `ohbs-image cleanup-runs` |

</details>

Part of [oh baseline](https://github.com/susunola): [ohbs-host](https://github.com/susunola/ohbs-host) · [ohbs-cloud](https://github.com/susunola/ohbs-cloud).
