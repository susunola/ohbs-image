<p align="center">
  <img src="docs/ohbs-image-logo.png" alt="ohbs-image" width="320">
</p>

<p align="center">
  <a href="README.md">English</a> · <b>简体中文</b> · <a href="README.ja.md">日本語</a> · <a href="README.th.md">ภาษาไทย</a>
</p>

# ohbs-image

通过 TOML 配置，在腾讯云构建经过 CIS 安全加固的 Linux 和 Windows 镜像。

自动创建临时实例、执行内置加固规则、复核审计结果并生成自定义镜像。支持 Packer 和腾讯云 Linux 自研引擎，提供逐条规则报告与可配置的质量门槛。

## 安装

需要 Python 3.11+。从仓库安装可使用当前自研引擎：

```bash
git clone https://github.com/susunola/ohbs-image.git
cd ohbs-image
pip install .
ohbs-image guide
```

自研 Linux 引擎需要 OpenSSH；Packer 构建需要 Packer 1.12+。Windows 还需要 Ansible、`ansible.windows` 和 `pywinrm`。详见[依赖说明](README.reference.md#prerequisites)。

## 快速开始

无需云账号，先运行离线演示：

```bash
ohbs-image try
```

实际构建时，通过环境变量提供凭据，并配置源镜像和网络：

```bash
export TENCENTCLOUD_SECRET_ID="<secret-id>"
export TENCENTCLOUD_SECRET_KEY="<secret-key>"

ohbs-image configure
ohbs-image doctor
ohbs-image plan
ohbs-image build --builder native   # 腾讯云 Linux
# ohbs-image build --builder packer # Linux 或 Windows
```

云构建会产生费用。运行端应有稳定出口，SSH/WinRM 仅向指定来源开放。构建前核对实际系统、源镜像与 Benchmark；Windows 构建还需在进程环境中设置强随机 `WINRM_PASSWORD`。

## 支持系统

共 14 个系统配置，支持 L1/L2：

| 系统 | 版本 |
|---|---|
| Ubuntu | 20.04、22.04、24.04 |
| RHEL | 8、9、10 |
| Rocky Linux | 9、10 |
| TencentOS | 3、4 |
| Windows Server | 2016、2019、2022、2025 |

构建成功、审计通过率、规则覆盖率和成品启动验收分别统计。提供系统配置不代表全部规则已验证通过；人工检查和缺失证据需要单独复核，得分不等同于 CIS 认证。

## 文档

- [完整中文参考](README.reference.zh-CN.md) · [完整英文参考](README.reference.md)
- [测试矩阵与源镜像](tests/TEST-MATRIX.md)
- [规则质量](docs/cis-rule-quality.md) · [证据索引](docs/public-evidence-index.md)
- [故障排查](docs/troubleshooting-support.md) · [安全模型](README.reference.md#security-model-for-enterprise-review)
- [更新日志](CHANGELOG.md) · [许可证](LICENSE)

属于 [oh baseline](https://github.com/susunola) 系列：[ohbs-host](https://github.com/susunola/ohbs-host) · [ohbs-cloud](https://github.com/susunola/ohbs-cloud)。
