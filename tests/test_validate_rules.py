"""Tests for scripts/validate_rules.py — the rule-catalog data-quality gate.

These build synthetic role trees under a tmp_path (mirroring
tests/test_catalog_resolution.py) so they need no cloud access and no real
catalogs.  The validator imports cleanly as a script, so we exec it via
runpy with argv.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import pytest


def _write_catalog(repo_root: Path, role: str, rules: list, guidance=None) -> Path:
    # Layout mirrors the real repo: <repo_root>/ohbs_image/roles/<role>/files/
    files = repo_root / "ohbs_image" / "roles" / role / "files"
    files.mkdir(parents=True, exist_ok=True)
    (files / "rules.json").write_text(__import__("json").dumps(rules, indent=2), encoding="utf-8")
    if guidance is not None:
        (files / "guidance.json").write_text(
            __import__("json").dumps(guidance, indent=2), encoding="utf-8")
    return files


def _run(repo_root: Path, *argv: str) -> tuple[int, str]:
    import scripts.validate_rules as vr  # noqa: E402
    vr.REPO_ROOT = repo_root
    import io
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        rc = vr.main(list(argv))
    finally:
        sys.stdout = old
    return rc, buf.getvalue()


# --- fixtures ---------------------------------------------------------------
GOOD_RULE = {
    "id": "1.1.1", "title": "Ensure foo", "section": "1.1",
    "levels": [1], "platforms": ["Server"], "assessment": "Automated",
    "family": "kv_conf", "risk": "low", "page": 10,
    "params": {"key": "x", "expected": 1, "op": "ge"}, "automated": True,
}


@pytest.fixture
def good_tree(tmp_path):
    root = tmp_path
    _write_catalog(root, "cis-rhel9", [dict(GOOD_RULE)])
    return root


# --- tests ------------------------------------------------------------------
def test_valid_catalog_passes(good_tree):
    rc, out = _run(good_tree)
    assert rc == 0, out
    assert "valid" in out


def test_missing_required_field_is_error(good_tree):
    bad = dict(GOOD_RULE)
    del bad["family"]
    _write_catalog(good_tree, "cis-rhel9", [bad])
    rc, out = _run(good_tree)
    assert rc == 1
    assert "missing required field 'family'" in out


def test_bad_type_is_error(good_tree):
    bad = dict(GOOD_RULE, page="ten")  # page must be int
    _write_catalog(good_tree, "cis-rhel9", [bad])
    rc, out = _run(good_tree)
    assert rc == 1
    assert "'page' must be an integer" in out


def test_invalid_level_enum_is_error(good_tree):
    bad = dict(GOOD_RULE, levels=[1, 3])  # 3 not in {1,2}
    _write_catalog(good_tree, "cis-rhel9", [bad])
    rc, out = _run(good_tree)
    assert rc == 1
    assert "levels' contains invalid value" in out


def test_invalid_risk_enum_is_error(good_tree):
    bad = dict(GOOD_RULE, risk="critical")  # not in VALID_RISK
    _write_catalog(good_tree, "cis-rhel9", [bad])
    rc, out = _run(good_tree)
    assert rc == 1
    assert "'risk' must be one of" in out


def test_duplicate_id_is_error(good_tree):
    _write_catalog(good_tree, "cis-rhel9", [dict(GOOD_RULE), dict(GOOD_RULE)])
    rc, out = _run(good_tree)
    assert rc == 1
    assert "duplicate id" in out


def test_empty_levels_is_error(good_tree):
    bad = dict(GOOD_RULE, levels=[])
    _write_catalog(good_tree, "cis-rhel9", [bad])
    rc, out = _run(good_tree)
    assert rc == 1
    assert "'levels' must not be empty" in out


def test_missing_automated_is_warning_not_error(good_tree):
    rule = dict(GOOD_RULE)
    del rule["automated"]
    _write_catalog(good_tree, "cis-rhel9", [rule])
    rc, out = _run(good_tree, "--verbose")  # default: warn, not error
    assert rc == 0, out
    assert "missing 'automated'" in out


def test_missing_automated_is_error_under_strict(good_tree):
    rule = dict(GOOD_RULE)
    del rule["automated"]
    _write_catalog(good_tree, "cis-rhel9", [rule])
    rc, out = _run(good_tree, "--strict")
    assert rc == 1
    assert "missing 'automated'" in out


def test_backfill_derives_automated_correctly(good_tree):
    auto = dict(GOOD_RULE, assessment="Automated", risk="low")
    manual = dict(GOOD_RULE, id="1.1.2", assessment="Manual", risk="low")
    none_risk = dict(GOOD_RULE, id="1.1.3", assessment="Automated", risk="none")
    for r in (auto, manual, none_risk):
        r.pop("automated", None)
    _write_catalog(good_tree, "cis-rhel9", [auto, manual, none_risk])
    rc, out = _run(good_tree, "--write-backfill")
    assert rc == 0, out
    import json
    rewritten = json.loads(
        (good_tree / "ohbs_image" / "roles" / "cis-rhel9" / "files" / "rules.json").read_text())
    by_id = {r["id"]: r for r in rewritten}
    assert by_id["1.1.1"]["automated"] is True
    assert by_id["1.1.2"]["automated"] is False   # Manual -> not automated
    assert by_id["1.1.3"]["automated"] is False   # risk=none -> not automated


def test_guidance_dict_shape_cross_check(good_tree):
    # Linux shape: guidance is a dict keyed by rule id.
    _write_catalog(good_tree, "cis-rhel9", [dict(GOOD_RULE)],
                   guidance={"1.1.1": {"description": "d", "rationale": "r"}})
    rc, out = _run(good_tree)
    assert rc == 0, out


def test_guidance_list_shape_stale_id_is_error(good_tree):
    # Windows shape: guidance is a list of {id,...}; a stale id must error.
    _write_catalog(good_tree, "cis-rhel9", [dict(GOOD_RULE)],
                   guidance=[{"id": "1.1.1", "remediation_hint": "x"},
                             {"id": "9.9.9", "remediation_hint": "stale"}])
    rc, out = _run(good_tree)
    assert rc == 1
    assert "guidance references id absent from rules.json" in out


def test_malformed_guidance_type_is_error(good_tree):
    # guidance must be object or array; a bare string is invalid.
    _write_catalog(good_tree, "cis-rhel9", [dict(GOOD_RULE)], guidance="not-an-object")
    rc, out = _run(good_tree)
    assert rc == 1
    assert "guidance.json must be a JSON object or array" in out
