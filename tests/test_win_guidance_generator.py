"""Regression tests for scripts/generate_win_guidance.py.

The Windows remediation hints must stay actionable (real GPO locations in
the project's own words, never the old "Configure via GPO: <title>"
placeholder) and the generator must be idempotent so bulk regeneration is
a no-op on a committed state.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("role", [
    "cis-win2016", "cis-win2019", "cis-win2022", "cis-win2025"])
def test_no_placeholder_hints_remain(role):
    """Every Windows guidance entry must carry a real, actionable hint —
    the old template ("Configure via GPO: <title>") is the bug this
    generator exists to prevent."""
    guidance = json.loads(
        (ROOT / "ohbs_image" / "roles" / role / "files" / "guidance.json")
        .read_text(encoding="utf-8"))
    assert guidance, f"{role}: guidance must be non-empty"
    for entry in guidance:
        hint = entry.get("remediation_hint", "")
        assert hint, f"{role}:{entry['id']} has no remediation hint"
        assert "Configure via GPO" not in hint, \
            f"{role}:{entry['id']} still has the placeholder hint"
        # Every hint points at a Group Policy / organizational location.
        assert ("Computer Configuration" in hint
                or "Manual / organizational" in hint), \
            f"{role}:{entry['id']} hint has no location: {hint!r}"


@pytest.mark.parametrize("role", [
    "cis-win2016", "cis-win2019", "cis-win2022", "cis-win2025"])
def test_guidance_matches_rules(role):
    """The guidance entries must mirror rules.json 1:1 (the generator
    consumes rules.json; a rule added without regenerating guidance would
    fail here)."""
    rules = json.loads(
        (ROOT / "ohbs_image" / "roles" / role / "files" / "rules.json")
        .read_text(encoding="utf-8"))
    guidance = json.loads(
        (ROOT / "ohbs_image" / "roles" / role / "files" / "guidance.json")
        .read_text(encoding="utf-8"))
    rule_ids = {str(r["id"]) for r in rules}
    guidance_ids = {str(e["id"]) for e in guidance}
    assert rule_ids == guidance_ids


def test_generator_is_idempotent():
    """Re-running the generator on a committed state must not change any
    file — otherwise a stray edit to rules.json would silently re-drift."""
    proc = subprocess.run(
        [sys.executable, "scripts/generate_win_guidance.py"],
        cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    lines = [ln for ln in proc.stdout.splitlines() if ln]
    assert len(lines) == 5  # 4 roles + "done:" summary
    for line in lines[:-1]:
        assert "0 hints updated" in line, f"generator not idempotent: {line}"


def test_known_sections_map_to_gpo_paths():
    """Spot-check the section -> GPO path mapping for representative rules."""
    guidance = json.loads(
        (ROOT / "ohbs_image" / "roles" / "cis-win2016" / "files"
         / "guidance.json").read_text(encoding="utf-8"))
    by_id = {str(e["id"]): e for e in guidance}
    assert "Password Policy" in by_id["1.1.1"]["remediation_hint"]
    assert "User Rights Assignment" in by_id["2.2.1"]["remediation_hint"]
    assert "Security Options" in by_id["2.3.1.1"]["remediation_hint"]
    assert "Advanced Audit Policy Configuration" in \
        by_id["17.5.1"]["remediation_hint"]
    assert "Event Log Service" in \
        by_id["18.10.26.1.2"]["remediation_hint"]
