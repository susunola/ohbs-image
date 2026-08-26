from pathlib import Path


def test_native_provider_is_read_only_and_fail_closed():
    root = Path("integrations/terraform-provider")
    provider = (root / "internal/provider/provider.go").read_text(encoding="utf-8")
    client = (root / "internal/provider/client.go").read_text(encoding="utf-8")
    assert "ohbsimage_channel" in (root / "README.md").read_text(encoding="utf-8")
    assert "Resources(_ context.Context) []func() resource.Resource { return nil }" in provider
    assert 'resolved.Artifact.Status != "active"' in client
    assert 'request.Header.Set("Authorization", "Bearer "+client.Token)' in client


def test_ci_templates_fail_closed_and_preserve_admission_evidence():
    github = Path("integrations/ci/github-actions-consumer.yml").read_text(encoding="utf-8")
    gitlab = Path("integrations/ci/gitlab-consumer.yml").read_text(encoding="utf-8")
    for text in (github, gitlab):
        assert "consumer resolve" in text
        assert "admission.json" in text
        assert '"active"' in text


def test_gitops_lock_is_generation_and_hash_pinned():
    schema = Path("integrations/gitops/golden-image-lock.schema.json").read_text(encoding="utf-8")
    assert '"generation"' in schema and '"admission_hash"' in schema
    assert '"additionalProperties": false' in schema


def test_compatibility_contract_covers_every_supported_consumer():
    contract = Path("docs/consumer-compatibility.md").read_text(encoding="utf-8")
    for consumer in ("Terraform native", "Terraform external", "GitHub Actions",
                     "GitLab CI", "OPA", "GitOps"):
        assert consumer in contract
    assert "reject unknown schema versions" in contract
