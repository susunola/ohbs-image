from __future__ import annotations

import argparse
import json
import os
import stat
from datetime import UTC, datetime, timedelta

from ohbs_image._state import (
    cmd_state_init,
    cmd_state_path,
    cmd_state_prune,
    cmd_state_status,
    cmd_state_sync,
)


def _lineage(root, rows):
    path = root / "lineage.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows),
                    encoding="utf-8")
    return path


def _rows():
    now = datetime.now(UTC)
    return [
        {"ts": (now - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ"),
         "run_id": "run-old", "status": "ok", "mode": "build",
         "profile": "tencentos3", "cis_level": 1, "score": 95.0,
         "image_name": "img-a", "region": "ap-guangzhou"},
        {"ts": (now - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
         "run_id": "run-mid", "status": "ok", "mode": "build",
         "profile": "ubuntu2404", "cis_level": 2, "score": 92.0,
         "image_name": "img-b", "region": "ap-singapore"},
        {"ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
         "run_id": "run-new", "status": "failed", "mode": "build",
         "profile": "tencentos3", "cis_level": 1, "score": None,
         "image_name": "", "region": "ap-guangzhou"},
    ]


def _make_evidence(root, run_ids):
    """Create per-run evidence files mirroring the real layout."""
    for directory in ("runs", "plans", "provenance", "releases"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    for run_id in run_ids:
        (root / "runs" / f"{run_id}.json").write_text('{"status": "ok"}', encoding="utf-8")
        (root / "plans" / f"{run_id}-plan.json").write_text('{"kind": "plan"}', encoding="utf-8")
        prov = root / "provenance" / f"img-x.{run_id}.provenance.json"
        prov.write_text('{"predicateType": "x"}', encoding="utf-8")
        prov.with_name(prov.name + ".sig").write_text("sig", encoding="utf-8")
    (root / "releases" / "img-1.json").write_text('{"state": "approved"}', encoding="utf-8")


def test_state_path_prints_absolute_root(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("ohbs_image._state._state_dir", lambda: tmp_path)
    assert cmd_state_path(argparse.Namespace()) == 0
    assert capsys.readouterr().out.strip() == str(tmp_path.resolve())


class TestStateStatus:
    def test_missing_root_reports_not_exists(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr("ohbs_image._state._state_dir", lambda: tmp_path / "absent")
        assert cmd_state_status(argparse.Namespace(output="json")) == 0
        doc = json.loads(capsys.readouterr().out)
        assert doc["schema"].endswith("/state-status/v1")
        assert doc["exists"] is False

    def test_counts_all_buckets(self, tmp_path, monkeypatch, capsys):
        _lineage(tmp_path, _rows())
        _make_evidence(tmp_path, ["run-old", "run-mid"])
        monkeypatch.setattr("ohbs_image._state._state_dir", lambda: tmp_path)
        assert cmd_state_status(argparse.Namespace(output="json")) == 0
        doc = json.loads(capsys.readouterr().out)
        assert doc["counts"]["lineage"] == 3
        assert doc["counts"]["runs"] == 2
        assert doc["counts"]["plans"] == 2
        assert doc["counts"]["releases"] == 1
        assert doc["counts"]["provenance"] == 2  # .sig files are not counted
        assert doc["counts"]["reports"] == 0
        assert doc["bytes"] > 0
        assert doc["last_record"] == _rows()[-1]["ts"]

    def test_text_output_lists_buckets(self, tmp_path, monkeypatch, capsys):
        _lineage(tmp_path, _rows())
        monkeypatch.setattr("ohbs_image._state._state_dir", lambda: tmp_path)
        assert cmd_state_status(argparse.Namespace(output="text")) == 0
        out = capsys.readouterr().out
        assert "evidence root:" in out and "lineage" in out and "reports" in out


class TestStateInit:
    def test_creates_layout_and_is_idempotent(self, tmp_path, monkeypatch, capsys):
        root = tmp_path / "ev"
        monkeypatch.setattr("ohbs_image._state._state_dir", lambda: root)
        assert cmd_state_init(argparse.Namespace()) == 0
        assert (root / "lineage.jsonl").is_file()
        for name in ("plans", "runs", "releases", "provenance", "reports"):
            assert (root / name).is_dir()
        mode = stat.S_IMODE(os.stat(root).st_mode)
        assert mode & 0o700 == 0o700
        # second run is a no-op success
        assert cmd_state_init(argparse.Namespace()) == 0

    def test_preserves_existing_evidence(self, tmp_path, monkeypatch):
        root = tmp_path / "ev"
        monkeypatch.setattr("ohbs_image._state._state_dir", lambda: root)
        _lineage(root, _rows())
        assert cmd_state_init(argparse.Namespace()) == 0
        assert _rows()[0]["run_id"] in (root / "lineage.jsonl").read_text(encoding="utf-8")


class TestStatePrune:
    def _args(self, **overrides):
        base = {"keep": 0, "older_than": 0, "dry_run": False, "output": "json"}
        base.update(overrides)
        return argparse.Namespace(**base)

    def test_keep_retains_newest_only(self, tmp_path, monkeypatch, capsys):
        _lineage(tmp_path, _rows())
        _make_evidence(tmp_path, ["run-old", "run-mid", "run-new"])
        monkeypatch.setattr("ohbs_image._state._state_dir", lambda: tmp_path)
        assert cmd_state_prune(self._args(keep=1)) == 0
        doc = json.loads(capsys.readouterr().out)
        assert doc["lineage_before"] == 3 and doc["lineage_after"] == 1
        assert doc["removed_runs"] == ["run-mid", "run-old"]
        lines = (tmp_path / "lineage.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1 and json.loads(lines[0])["run_id"] == "run-new"
        # per-run evidence dies with its record
        assert not (tmp_path / "runs" / "run-old.json").exists()
        assert not (tmp_path / "plans" / "run-mid-plan.json").exists()
        assert not list((tmp_path / "provenance").glob("*.run-old.provenance.json"))
        # the permanent approval trail survives
        assert (tmp_path / "releases" / "img-1.json").exists()

    def test_older_than_drops_old_records(self, tmp_path, monkeypatch, capsys):
        _lineage(tmp_path, _rows())
        _make_evidence(tmp_path, ["run-old", "run-mid"])
        monkeypatch.setattr("ohbs_image._state._state_dir", lambda: tmp_path)
        assert cmd_state_prune(self._args(older_than=5)) == 0
        doc = json.loads(capsys.readouterr().out)
        assert doc["removed_runs"] == ["run-old"]
        lines = (tmp_path / "lineage.jsonl").read_text(encoding="utf-8").splitlines()
        assert [json.loads(line)["run_id"] for line in lines] == ["run-mid", "run-new"]

    def test_keep_and_older_than_compose(self, tmp_path, monkeypatch, capsys):
        _lineage(tmp_path, _rows())
        monkeypatch.setattr("ohbs_image._state._state_dir", lambda: tmp_path)
        assert cmd_state_prune(self._args(keep=1, older_than=1)) == 0
        doc = json.loads(capsys.readouterr().out)
        assert doc["lineage_after"] == 1
        assert doc["removed_runs"] == ["run-mid", "run-old"]

    def test_dry_run_changes_nothing(self, tmp_path, monkeypatch, capsys):
        _lineage(tmp_path, _rows())
        _make_evidence(tmp_path, ["run-old", "run-mid", "run-new"])
        before = (tmp_path / "lineage.jsonl").read_bytes()
        monkeypatch.setattr("ohbs_image._state._state_dir", lambda: tmp_path)
        assert cmd_state_prune(self._args(keep=1, dry_run=True)) == 0
        doc = json.loads(capsys.readouterr().out)
        assert doc["dry_run"] is True and doc["removed_runs"] == ["run-mid", "run-old"]
        assert (tmp_path / "lineage.jsonl").read_bytes() == before
        assert (tmp_path / "runs" / "run-old.json").exists()

    def test_no_criteria_is_an_error(self, tmp_path, monkeypatch, caplog):
        import logging

        from ohbs_image._logging import logger
        logger.setLevel(logging.INFO)
        monkeypatch.setattr("ohbs_image._state._state_dir", lambda: tmp_path)
        assert cmd_state_prune(self._args()) == 1
        assert "nothing to prune" in caplog.text

    def test_nothing_to_prune_when_all_kept(self, tmp_path, monkeypatch, capsys):
        _lineage(tmp_path, _rows())
        monkeypatch.setattr("ohbs_image._state._state_dir", lambda: tmp_path)
        assert cmd_state_prune(self._args(keep=99)) == 0
        doc = json.loads(capsys.readouterr().out)
        assert doc["lineage_after"] == 3 and doc["removed_runs"] == []


class TestStateSyncCheck:
    def test_check_lists_transfers_without_copying(self, tmp_path, monkeypatch, capsys):
        root = tmp_path / "root"
        remote = tmp_path / "remote"
        _lineage(root, _rows())
        (root / "runs" / "run-new.json").parent.mkdir(parents=True, exist_ok=True)
        (root / "runs" / "run-new.json").write_text("{}", encoding="utf-8")
        (remote / "stale.txt").parent.mkdir(parents=True, exist_ok=True)
        (remote / "stale.txt").write_text("stale", encoding="utf-8")
        monkeypatch.setattr("ohbs_image._state._state_dir", lambda: root)
        args = argparse.Namespace(backend="local", location=str(remote),
                                  direction="push", check=True)
        assert cmd_state_sync(args) == 0
        out = capsys.readouterr().out
        assert "+ lineage.jsonl" in out and "+ runs/run-new.json" in out
        # nothing was actually copied
        assert not (remote / "lineage.jsonl").exists()

    def test_check_rejects_cos_backend(self, tmp_path, monkeypatch, caplog):
        import logging

        from ohbs_image._logging import logger
        logger.setLevel(logging.INFO)
        monkeypatch.setattr("ohbs_image._state._state_dir", lambda: tmp_path)
        args = argparse.Namespace(backend="cos", location="cos://bucket/x",
                                  direction="push", check=True)
        assert cmd_state_sync(args) == 1
        assert "only supported for the local backend" in caplog.text

    def test_sync_push_local_copies(self, tmp_path, monkeypatch, caplog):
        import logging

        from ohbs_image._logging import logger
        logger.setLevel(logging.INFO)
        root = tmp_path / "root"
        remote = tmp_path / "remote"
        _lineage(root, _rows())
        monkeypatch.setattr("ohbs_image._state._state_dir", lambda: root)
        args = argparse.Namespace(backend="local", location=str(remote),
                                  direction="push", check=False)
        assert cmd_state_sync(args) == 0
        assert (remote / "lineage.jsonl").is_file()
        assert "State push complete" in caplog.text
