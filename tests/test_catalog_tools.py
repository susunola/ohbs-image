from __future__ import annotations

import argparse
import json

from ohbs_image._catalog_tools import cmd_catalog_list, cmd_catalog_verify
from ohbs_image._profiles import PROFILES


def _args(**overrides):
    base = {"output": "text"}
    base.update(overrides)
    return argparse.Namespace(**base)


class TestCatalogList:
    def test_json_contract_covers_all_profiles(self, capsys):
        assert cmd_catalog_list(_args(output="json")) == 0
        doc = json.loads(capsys.readouterr().out)
        assert doc["schema"].endswith("/catalog-list/v1")
        assert len(doc["catalogs"]) == len(PROFILES)
        for item in doc["catalogs"]:
            assert item["rules"] > 0
            assert len(item["sha256"]) == 64
            assert item["catalog"] in ("rules.json",)
        assert doc["catalogs"][0]["profile"] == "rhel10"

    def test_text_output_has_columns(self, capsys):
        assert cmd_catalog_list(_args()) == 0
        out = capsys.readouterr().out
        assert "profile" in out and "rules" in out and "sha256" in out
        assert "tencentos3" in out and "win2016" in out


class TestCatalogVerify:
    def test_all_bundled_catalogs_pass(self, caplog):
        import logging

        from ohbs_image._logging import logger
        logger.setLevel(logging.INFO)
        assert cmd_catalog_verify(_args()) == 0
        assert "tencentos3" in caplog.text and "win2022" in caplog.text

    def test_json_contract(self, capsys):
        assert cmd_catalog_verify(_args(output="json")) == 0
        doc = json.loads(capsys.readouterr().out)
        assert doc["schema"].endswith("/catalog-verify/v1")
        assert doc["ok"] is True
        assert all(item["ok"] for item in doc["catalogs"])
        assert all(item["errors"] == [] for item in doc["catalogs"])

    def test_broken_rules_json_fails(self, tmp_path, monkeypatch, caplog):
        import logging

        from ohbs_image._logging import logger
        logger.setLevel(logging.INFO)
        bad = tmp_path / "rules.json"
        bad.write_text("{not valid json", encoding="utf-8")
        monkeypatch.setattr("ohbs_image._catalog_tools._catalog_path",
                            lambda role, benchmark: bad)
        assert cmd_catalog_verify(_args()) == 1
        assert "not valid JSON" in caplog.text

    def test_missing_catalog_fails(self, tmp_path, monkeypatch, caplog):
        import logging

        from ohbs_image._logging import logger
        logger.setLevel(logging.INFO)
        missing = tmp_path / "rules.json"
        monkeypatch.setattr("ohbs_image._catalog_tools._catalog_path",
                            lambda role, benchmark: missing)
        assert cmd_catalog_verify(_args()) == 1
        assert "missing" in caplog.text

    def test_guidance_unknown_reference_is_warning_by_default(self, tmp_path, monkeypatch, caplog):
        import logging

        from ohbs_image._logging import logger
        logger.setLevel(logging.INFO)
        catalog = tmp_path / "rules.json"
        catalog.write_text(json.dumps([{"id": "1.1.1", "title": "x"}]), encoding="utf-8")
        (tmp_path / "guidance.json").write_text(
            json.dumps({"9.9.9": {"description": "ghost rule"}}), encoding="utf-8")
        monkeypatch.setattr("ohbs_image._catalog_tools._catalog_path",
                            lambda role, benchmark: catalog)
        # drift is surfaced but does not break the default gate
        assert cmd_catalog_verify(_args()) == 0
        assert "unknown rule 9.9.9" in caplog.text

    def test_strict_mode_fails_on_unknown_reference(self, tmp_path, monkeypatch, caplog):
        import logging

        from ohbs_image._logging import logger
        logger.setLevel(logging.INFO)
        catalog = tmp_path / "rules.json"
        catalog.write_text(json.dumps([{"id": "1.1.1", "title": "x"}]), encoding="utf-8")
        (tmp_path / "guidance.json").write_text(
            json.dumps({"9.9.9": {"description": "ghost rule"}}), encoding="utf-8")
        monkeypatch.setattr("ohbs_image._catalog_tools._catalog_path",
                            lambda role, benchmark: catalog)
        assert cmd_catalog_verify(_args(strict=True)) == 1
        assert "unknown rule 9.9.9" in caplog.text

    def test_rule_without_id_fails(self, tmp_path, monkeypatch, caplog):
        import logging

        from ohbs_image._logging import logger
        logger.setLevel(logging.INFO)
        catalog = tmp_path / "rules.json"
        catalog.write_text(json.dumps([{"title": "no id"}]), encoding="utf-8")
        monkeypatch.setattr("ohbs_image._catalog_tools._catalog_path",
                            lambda role, benchmark: catalog)
        assert cmd_catalog_verify(_args()) == 1
        assert "no id" in caplog.text
