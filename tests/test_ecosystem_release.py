from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_compatibility_matrix_has_all_release_surfaces() -> None:
    matrix = json.loads((ROOT / "integrations/compatibility-matrix.json").read_text())
    assert matrix["provider_api"] == "1.x"
    assert matrix["extension_api"] == "1.x"
    assert len(matrix["terraform_provider"]["platforms"]) == 6


def test_release_and_template_assets_are_complete() -> None:
    paths = [
        ".github/workflows/terraform-provider-release.yml",
        "integrations/terraform-provider/.goreleaser.yml",
        "integrations/extension-template/pyproject.toml",
        "integrations/extension-template/ohbs_image_example_scanner.py",
        "docs/ecosystem-release.md",
    ]
    assert all((ROOT / path).is_file() for path in paths)
    workflow = (ROOT / paths[0]).read_text()
    assert "go test ./..." in workflow
    assert "attest-build-provenance" in workflow
    assert "TERRAFORM_REGISTRY_GPG_PRIVATE_KEY" in workflow
