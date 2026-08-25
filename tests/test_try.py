from __future__ import annotations

import argparse
import json
import logging

import ohbs_image
from ohbs_image._try import _demo_status, cmd_try

# A tiny synthetic catalog so the demo audit + rendered report are hermetic
# (no dependency on the bundled rule data or its size).
CATALOG = [
    {"id": "1.1.1", "title": "Ensure filesystem mounts", "section": "1.1",
     "levels": [1, 2], "assessment": "Automated"},
    {"id": "1.2.3", "title": "Ensure package updates", "section": "1.2",
     "levels": [1, 2], "assessment": "Automated"},
    {"id": "5.4.3.2", "title": "Ensure ssh MaxAuthTries", "section": "5.4",
     "levels": [1, 2], "assessment": "Manual"},
]
GUIDANCE = {r["id"]: {"remediation": "fix it", "rationale": "why",
                      "impact": "low"} for r in CATALOG}


def _stub_gates(monkeypatch):
    """Make `try` fully offline: gates pass, catalog comes from the stub."""
    monkeypatch.setattr("ohbs_image._try.cmd_engine_verify", lambda args: 0)
    monkeypatch.setattr("ohbs_image._try.cmd_catalog_verify", lambda args: 0)
    # cmd_try loads the catalog for the demo audit...
    monkeypatch.setattr("ohbs_image._try._load_report_catalog",
                        lambda ctx: (CATALOG, GUIDANCE))
    # ...and the HTML renderer loads it again for the per-rule rows.
    monkeypatch.setattr("ohbs_image._reports._load_report_catalog",
                        lambda ctx: (CATALOG, GUIDANCE))


class TestDemoStatus:
    def test_deterministic_derivation(self):
        # Digit sums: 1.1.1 -> 3 (fail), 1.2.3 -> 6 (manual), 5.4.3.2 -> 14 (pass)
        assert _demo_status("1.1.1") == "fail"
        assert _demo_status("1.2.3") == "manual"
        assert _demo_status("5.4.3.2") == "pass"

    def test_no_digits_passes(self):
        assert _demo_status("section-a") == "pass"


class TestCmdTry:
    def test_success_writes_three_artifacts(self, tmp_path, monkeypatch, capsys,
                                            caplog):
        _stub_gates(monkeypatch)
        # `ok()` logs at INFO (not stdout), so the summary lives in the log.
        caplog.set_level(logging.INFO)
        out_dir = tmp_path / "out"
        assert cmd_try(argparse.Namespace(
            profile="tencentos3", level=1, output=str(out_dir))) == 0
        assert "Demo complete: 3 rules" in caplog.text

        config = out_dir / "ohbs-image.toml"
        assert config.is_file()
        assert "[build]" in config.read_text(encoding="utf-8")

        audit = json.loads((out_dir / "demo-audit.json").read_text(
            encoding="utf-8"))
        assert audit["mode"] == "demo"
        # benchmark comes from the profile metadata (e.g. "CIS-v1.0.0")
        assert audit["benchmark"].startswith("CIS")
        assert audit["summary"]["all"] == {"pass": 1, "fail": 1,
                                           "manual": 1, "error": 0}
        # The pseudo-audit is a pure function of the rule IDs.
        assert {r["status"] for r in audit["results"]} == {"pass", "fail", "manual"}

        report = out_dir / "demo-report.html"
        assert report.is_file()
        page = report.read_text(encoding="utf-8")
        assert "<!doctype html" in page
        assert "Ensure ssh MaxAuthTries" in page
        assert "demo-tencentos3" in page  # demo-{profile} title + cover meta

    def test_repeat_run_is_identical(self, tmp_path, monkeypatch):
        # Determinism is a feature: re-running must produce identical audit
        # JSON (the HTML report embeds a timestamp, so compare audit only).
        _stub_gates(monkeypatch)
        assert cmd_try(argparse.Namespace(
            profile="tencentos3", level=2, output=str(tmp_path / "run1"))) == 0
        assert cmd_try(argparse.Namespace(
            profile="tencentos3", level=2, output=str(tmp_path / "run2"))) == 0
        first = (tmp_path / "run1" / "demo-audit.json").read_text(encoding="utf-8")
        second = (tmp_path / "run2" / "demo-audit.json").read_text(encoding="utf-8")
        assert first == second

    def test_unknown_profile_fails(self, tmp_path, monkeypatch, caplog):
        assert cmd_try(argparse.Namespace(
            profile="nope", level=1, output=str(tmp_path / "out"))) == 1
        assert "unknown profile" in caplog.text

    def test_engine_gate_failure_aborts(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setattr("ohbs_image._try.cmd_engine_verify", lambda args: 1)
        assert cmd_try(argparse.Namespace(
            profile="tencentos3", level=1, output=str(tmp_path / "out"))) == 1
        assert "engine gate failed" in caplog.text

    def test_try_flag_registered(self):
        parser = ohbs_image.build_parser()
        choices = parser._subparsers._group_actions[0].choices
        assert "try" in choices
        try_parser = choices["try"]
        assert {a.dest for a in try_parser._actions} >= {"output", "profile",
                                                         "level"}


class TestDemoMatchesRealCatalog:
    """The demo pseudo-audit must be a pure function of the *real* bundled
    rule catalog: if the shipped rules.json for a profile changes shape,
    ordering or membership, the demo audit must track it exactly. This is
    the offline equivalent of a golden-snapshot gate (no giant fixture file
    to maintain — the catalog itself is the source of truth)."""

    def _real_demo_audit(self, profile: str = "tencentos3",
                         level: int = 1) -> dict:
        from ohbs_image._profiles import PROFILES
        from ohbs_image._reports import _load_report_catalog, _ReportContext
        meta = PROFILES[profile]
        ctx = _ReportContext(
            profile_name=profile, level=level, region="demo-region",
            zone="demo-zone", source_image_id="img-demo-source",
            image_benchmark=str(meta["benchmark"]), run_id="try-demo",
            role_dir=str(meta["role_dir"]))
        catalog, _guidance = _load_report_catalog(ctx)
        assert catalog, "bundled catalog must be non-empty"
        results: list[dict] = []
        counts = {"pass": 0, "fail": 0, "manual": 0, "error": 0}
        for rule in catalog:
            status = _demo_status(str(rule.get("id", "")))
            counts[status] += 1
            results.append({
                "id": str(rule.get("id", "")),
                "title": rule.get("title", ""),
                "section": rule.get("section", ""),
                "levels": rule.get("levels", []),
                "assessment": rule.get("assessment", "Automated"),
                "status": status,
                "apply_status": ("applied" if status == "pass"
                                 else "skipped_manual" if status == "manual"
                                 else "apply_failed"),
            })
        return {"summary": {"all": counts}, "results": results}

    def test_try_audit_matches_real_catalog(self, tmp_path, monkeypatch,
                                            caplog):
        """Running `ohbs-image try` on the real bundled data must produce
        exactly the audit the catalog implies — no drift between the demo
        and the shipped rules."""
        import logging
        # Only stub the cloud-touching gates; the catalog is real.
        monkeypatch.setattr("ohbs_image._try.cmd_engine_verify", lambda args: 0)
        monkeypatch.setattr("ohbs_image._try.cmd_catalog_verify", lambda args: 0)
        caplog.set_level(logging.INFO)
        out_dir = tmp_path / "out"
        assert cmd_try(argparse.Namespace(
            profile="tencentos3", level=1, output=str(out_dir))) == 0
        audit = json.loads((out_dir / "demo-audit.json").read_text(
            encoding="utf-8"))
        expected = self._real_demo_audit()
        assert audit["summary"] == expected["summary"]
        assert audit["results"] == expected["results"]
        # Sanity: the real catalog is not the 3-rule synthetic stub.
        assert len(expected["results"]) > 100
