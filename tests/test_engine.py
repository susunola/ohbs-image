from __future__ import annotations

import argparse
import json

from ohbs_image._engine import cmd_engine_list, cmd_engine_verify, cmd_engine_version
from ohbs_image._profiles import PROFILES


def _args(**overrides):
    base = {"output": "text"}
    base.update(overrides)
    return argparse.Namespace(**base)


class TestEngineList:
    def test_json_contract_covers_all_profiles(self, capsys):
        assert cmd_engine_list(_args(output="json")) == 0
        doc = json.loads(capsys.readouterr().out)
        assert doc["schema"].endswith("/engine-list/v1")
        assert len(doc["engines"]) == len(PROFILES)
        for item in doc["engines"]:
            assert item["version"] != "-"
            assert len(item["sha256"]) == 64
            assert item["bytes"] > 0
            assert item["engine"] in ("ohbs_engine.py", "ohbs_engine.ps1")
        linux = [e for e in doc["engines"] if e["family"] == "linux"]
        windows = [e for e in doc["engines"] if e["family"] == "windows"]
        assert linux and windows

    def test_text_output_has_columns(self, capsys):
        assert cmd_engine_list(_args()) == 0
        out = capsys.readouterr().out
        assert "profile" in out and "sha256" in out and "engine" in out
        assert "tencentos3" in out and "win2016" in out


class TestEngineVerify:
    def test_all_bundled_engines_pass(self, caplog):
        import logging

        from ohbs_image._logging import logger
        logger.setLevel(logging.INFO)
        assert cmd_engine_verify(_args()) == 0
        assert "tencentos3" in caplog.text and "win2022" in caplog.text

    def test_json_contract(self, capsys):
        assert cmd_engine_verify(_args(output="json")) == 0
        doc = json.loads(capsys.readouterr().out)
        assert doc["schema"].endswith("/engine-verify/v1")
        assert doc["ok"] is True
        assert all(item["ok"] for item in doc["engines"])

    def test_broken_python_engine_fails(self, tmp_path, monkeypatch, caplog):
        import logging

        from ohbs_image._logging import logger
        logger.setLevel(logging.INFO)
        bad = tmp_path / "ohbs_engine.py"
        bad.write_text("def broken(:\n", encoding="utf-8")
        monkeypatch.setattr("ohbs_image._engine._engine_path",
                            lambda role: bad)
        assert cmd_engine_verify(_args()) == 1
        assert "syntax error" in caplog.text.lower()

    def test_empty_powershell_engine_fails(self, tmp_path, monkeypatch, caplog):
        import logging

        from ohbs_image._logging import logger
        logger.setLevel(logging.INFO)
        bad = tmp_path / "ohbs_engine.ps1"
        bad.write_text("   \n", encoding="utf-8")
        monkeypatch.setattr("ohbs_image._engine._engine_path",
                            lambda role: bad)
        assert cmd_engine_verify(_args()) == 1
        assert "empty" in caplog.text.lower()

    def test_missing_engine_fails(self, tmp_path, monkeypatch, caplog):
        import logging

        from ohbs_image._logging import logger
        logger.setLevel(logging.INFO)
        missing = tmp_path / "nope.py"
        monkeypatch.setattr("ohbs_image._engine._engine_path",
                            lambda role: missing)
        assert cmd_engine_verify(_args()) == 1
        assert "missing" in caplog.text.lower()


class TestEngineVersion:
    def test_prints_ohbs_and_family_versions(self, capsys):
        from ohbs_image._logging import VERSION
        assert cmd_engine_version(argparse.Namespace()) == 0
        out = capsys.readouterr().out
        assert f"ohbs-image {VERSION}" in out
        assert "engine (linux):" in out and "engine (windows):" in out


class TestListVersionsEngineColumn:
    def test_engine_version_is_the_bundled_engine_version(self, capsys):
        """H-段口径修正: list --versions 的 engine 列必须是引擎自身版本,
        而不是 ohbs-image 的版本号."""
        from ohbs_image._commands import cmd_list
        from ohbs_image._engine import _engine_path, _engine_version
        from ohbs_image._logging import VERSION
        assert cmd_list(_args(output="json", versions=True)) == 0
        doc = json.loads(capsys.readouterr().out)
        for entry in doc["profiles"]:
            role = {"ubuntu2404": "cis-ubuntu2404",
                    "win2016": "cis-win2016"}.get(entry["profile"])
            if role is None:
                continue
            expected = _engine_version(_engine_path(role))
            assert entry["engine_version"] == expected
            assert entry["engine_version"] != VERSION  # not the tool version
