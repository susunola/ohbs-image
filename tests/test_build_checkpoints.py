import argparse
import json

from ohbs_image._build_checkpoints import (
    cmd_run_checkpoints,
    read_build_checkpoints,
    verify_build_checkpoint,
    write_build_checkpoint,
)
from ohbs_image._config import load_config, resolve
from tests.test_launch import _config


def test_build_checkpoints_are_idempotent_and_hash_verified(tmp_path):
    config = tmp_path / "config.toml"
    _config(config)
    resolved = resolve(load_config(config))
    resolved.run_id = "run-1"
    write_build_checkpoint(resolved, "rendered", {"image_name": "gold"}, root=tmp_path)
    write_build_checkpoint(resolved, "rendered", {"image_name": "gold"}, root=tmp_path)
    write_build_checkpoint(resolved, "snapshot-created", {"image_ids": ["img-1"]}, root=tmp_path)
    doc = read_build_checkpoints("run-1", tmp_path)
    assert doc is not None
    assert doc["completed_phases"] == ["rendered", "snapshot-created"]
    assert verify_build_checkpoint("run-1", tmp_path) == []

    path = tmp_path / "checkpoints" / "run-1.json"
    tampered = json.loads(path.read_text())
    tampered["artifacts"]["image_ids"] = ["img-evil"]
    path.write_text(json.dumps(tampered))
    assert verify_build_checkpoint("run-1", tmp_path) == ["checkpoint document hash mismatch"]


def test_checkpoint_command_outputs_json(tmp_path, monkeypatch, capsys):
    config = tmp_path / "config.toml"
    _config(config)
    resolved = resolve(load_config(config))
    resolved.run_id = "run-2"
    write_build_checkpoint(resolved, "rendered", root=tmp_path)
    monkeypatch.setattr("ohbs_image._build_checkpoints._lineage_path",
                        lambda: tmp_path / "lineage.jsonl")
    assert cmd_run_checkpoints(argparse.Namespace(run_id="run-2", output="json")) == 0
    assert json.loads(capsys.readouterr().out)["completed_phases"] == ["rendered"]
