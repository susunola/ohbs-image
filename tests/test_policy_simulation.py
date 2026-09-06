from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime

from ohbs_image._policy import POLICY_SCHEMA
from ohbs_image._policy_simulation import (
    POLICY_SIMULATION_SCHEMA,
    cmd_policy_simulate,
    simulate_policy,
)
from ohbs_image._registry import _hash, put_artifact


def policy(score: int, version: str) -> dict:
    return {"schema": POLICY_SCHEMA, "policy_id": "production", "version": version,
            "defaults": {"allowed_status": ["active"], "min_score": score},
            "environments": {}, "exceptions": []}


def _artifact(artifact_id: str, score: int) -> dict:
    document = {"schema": "https://ohbs-image.dev/artifact/v1", "artifact_id": artifact_id,
                "status": "active", "score": score, "bucket": "linux", "version": "1",
                "created_at": "2026-01-01T00:00:00Z"}
    document["document_hash"] = _hash(document)
    return document


def test_simulation_reports_transitions_without_mutation() -> None:
    artifacts = [
        {"artifact_id": "good", "bucket": "linux", "version": "1", "status": "active",
         "score": 95, "created_at": "2026-01-01T00:00:00Z"},
        {"artifact_id": "borderline", "bucket": "linux", "version": "2", "status": "active",
         "score": 85, "created_at": "2026-01-01T00:00:00Z"},
    ]
    result = simulate_policy(policy(90, "2"), artifacts, "production",
                             baseline=policy(80, "1"),
                             now=datetime(2026, 1, 2, tzinfo=UTC))
    assert result["schema"] == POLICY_SIMULATION_SCHEMA
    assert result["dry_run"] is True
    assert result["summary"] == {"allowed": 1, "denied": 1, "newly_allowed": 0,
                                 "newly_denied": 1, "unchanged": 1}
    assert result["control_denials"] == {"score": 1}
    assert artifacts[1]["score"] == 85


def test_simulation_hash_is_independent_of_input_order() -> None:
    """Registry reads do not guarantee row order, but the result is hashed.

    Two runs over the same artifacts in a different order must produce one
    identical document, otherwise the digest is not evidence.
    """
    artifacts = [_artifact("good", 95), _artifact("borderline", 85)]
    forward = simulate_policy(policy(90, "2"), artifacts, "production",
                              baseline=policy(80, "1"),
                              now=datetime(2026, 1, 2, tzinfo=UTC))
    reversed_order = simulate_policy(policy(90, "2"), list(reversed(artifacts)),
                                     "production", baseline=policy(80, "1"),
                                     now=datetime(2026, 1, 2, tzinfo=UTC))
    assert forward["document_hash"] == reversed_order["document_hash"]
    assert [row["artifact_id"] for row in forward["artifacts"]] == ["borderline", "good"]


def _simulate_args(bundle: str, **overrides: object) -> argparse.Namespace:
    args = {"bundle": bundle, "environment": "production", "baseline": "",
            "artifact": [], "fail_on_newly_denied": False, "output": "json"}
    args.update(overrides)
    return argparse.Namespace(**args)


def test_policy_simulate_cli_reads_the_registry(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("OHBS_IMAGE_STATE_DIR", str(tmp_path / "state"))
    put_artifact(_artifact("img-good", 95))
    put_artifact(_artifact("img-weak", 70))

    candidate = tmp_path / "candidate.json"
    baseline = tmp_path / "baseline.json"
    candidate.write_text(json.dumps(policy(90, "2")), encoding="utf-8")
    baseline.write_text(json.dumps(policy(60, "1")), encoding="utf-8")

    args = _simulate_args(str(candidate), baseline=str(baseline))
    assert cmd_policy_simulate(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["artifact_count"] == 2
    assert result["summary"] == {"allowed": 1, "denied": 1, "newly_allowed": 0,
                                 "newly_denied": 1, "unchanged": 1}
    assert result["dry_run"] is True

    strict = _simulate_args(str(candidate), baseline=str(baseline),
                            fail_on_newly_denied=True)
    assert cmd_policy_simulate(strict) == 1
    capsys.readouterr()


def test_policy_simulate_cli_honours_selected_artifacts(tmp_path, capsys,
                                                        monkeypatch) -> None:
    monkeypatch.setenv("OHBS_IMAGE_STATE_DIR", str(tmp_path / "state"))
    put_artifact(_artifact("img-good", 95))
    put_artifact(_artifact("img-weak", 70))
    candidate = tmp_path / "candidate.json"
    candidate.write_text(json.dumps(policy(90, "2")), encoding="utf-8")

    args = _simulate_args(str(candidate), artifact=["img-good"])
    assert cmd_policy_simulate(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert [row["artifact_id"] for row in result["artifacts"]] == ["img-good"]


def test_policy_simulate_cli_fails_closed(tmp_path, caplog, monkeypatch) -> None:
    monkeypatch.setenv("OHBS_IMAGE_STATE_DIR", str(tmp_path / "state"))
    candidate = tmp_path / "candidate.json"
    candidate.write_text(json.dumps(policy(90, "2")), encoding="utf-8")

    # Nothing registered yet.
    with caplog.at_level("ERROR"):
        assert cmd_policy_simulate(_simulate_args(str(candidate))) == 2
    assert "no registered artifacts" in caplog.text

    # Referenced artifact does not exist.
    caplog.clear()
    with caplog.at_level("ERROR"):
        assert cmd_policy_simulate(_simulate_args(str(candidate),
                                                 artifact=["img-nope"])) == 2
    assert "not found" in caplog.text

    # Structurally invalid bundle.
    caplog.clear()
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps({"schema": "nope"}), encoding="utf-8")
    with caplog.at_level("ERROR"):
        assert cmd_policy_simulate(_simulate_args(str(broken))) == 2
    assert "invalid policy" in caplog.text
