"""Static guards for workflow context and cloud safety invariants."""
from __future__ import annotations

from pathlib import Path

WORKFLOWS = Path(".github/workflows")


def test_runner_context_is_not_used_in_job_level_environment() -> None:
    """The runner context exists only after a job starts.

    Referencing it from a job-level ``env`` makes GitHub reject the workflow
    before creating any jobs, which appears as a red run with ``jobs=[]``.
    """
    for name in ("cloud-canary.yml", "real-e2e.yml", "build-image.yml"):
        text = (WORKFLOWS / name).read_text(encoding="utf-8")
        assert "${{ runner.temp }}" not in text, name
        assert "${{ runner.tool_cache }}" not in text, name


def test_cloud_workflows_initialize_state_through_github_env() -> None:
    expected = {
        "cloud-canary.yml": "$RUNNER_TEMP/ohbs-image-canary-state",
        "real-e2e.yml": "$RUNNER_TOOL_CACHE/ohbs-image-e2e-state",
        "build-image.yml": "$RUNNER_TOOL_CACHE/ohbs-image-state",
    }
    for name, state_dir in expected.items():
        text = (WORKFLOWS / name).read_text(encoding="utf-8")
        assert f"OHBS_IMAGE_STATE_DIR={state_dir}" in text, name
        assert "${{ env.OHBS_IMAGE_STATE_DIR }}" in text, name


def test_acceptance_workflows_always_publish_and_enforce_result() -> None:
    for name in ("cloud-canary.yml", "real-e2e.yml"):
        text = (WORKFLOWS / name).read_text(encoding="utf-8")
        assert "id: acceptance" in text, name
        assert "continue-on-error: true" in text, name
        assert "scripts/write_acceptance_result.py" in text, name
        assert "acceptance-result.json" in text, name
        assert "steps.acceptance.outcome" in text, name
        assert "Enforce acceptance result" in text, name


def test_canary_covers_representative_linux_and_windows_profiles() -> None:
    text = (WORKFLOWS / "cloud-canary.yml").read_text(encoding="utf-8")
    for profile in ("tencentos3", "ubuntu2404", "win2022"):
        assert profile in text
