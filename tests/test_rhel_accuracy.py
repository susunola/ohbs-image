import json
from pathlib import Path
from types import SimpleNamespace

from tests.engine_fixtures import load_engine

ROLES = Path(__file__).parents[1] / "ohbs_image" / "roles"


def _rules(role: str) -> dict[str, dict]:
    payload = json.loads((ROLES / role / "files" / "rules.json").read_text())
    items = payload if isinstance(payload, list) else payload["rules"]
    return {item["id"]: item for item in items}


def test_rocky10_catalog_matches_verified_el10_control_set():
    """The supplied CIS Rocky Linux 10 v1.0.0 summary has 328 controls
    with IDs identical to the implemented EL10 catalog."""
    rocky = _rules("cis-rocky10")
    assert len(rocky) == 328
    assert set(rocky) == set(_rules("cis-rhel10"))


def test_rocky10_benchmark_identity_is_not_rhel10():
    defaults = (ROLES / "cis-rocky10" / "defaults" / "main.yml").read_text()
    assert 'cis_benchmark_name: "CIS Rocky Linux 10 Benchmark"' in defaults
    assert 'cis_benchmark_version: "v1.0.0"' in defaults


def test_rhel_sudo_logfile_uses_supported_equality_operator():
    for role in ("cis-rhel8", "cis-rhel9", "cis-rhel10", "cis-rocky10"):
        assert _rules(role)["5.2.3"]["params"]["op"] == "kv"


def test_rhel_journal_upload_is_deployment_conditional():
    ids = {"cis-rhel8": "6.2.1.2.3", "cis-rhel9": "6.2.2.1.3",
           "cis-rhel10": "6.2.2.1.3", "cis-rocky10": "6.2.2.1.3"}
    for role, rule_id in ids.items():
        assert _rules(role)[rule_id]["params"]["requires_config"] == "journal-upload"


def test_rhel_journald_only_rule_is_not_scored_with_rsyslog_active():
    ids = {"cis-rhel8": "6.2.1.1.4", "cis-rhel9": "6.2.2.2",
           "cis-rhel10": "6.2.2.2", "cis-rocky10": "6.2.2.2"}
    for role, rule_id in ids.items():
        assert _rules(role)[rule_id]["params"]["unless_service_active"] == \
            "rsyslog.service"


def test_rhel9_ptrace_accepts_cis_allowed_stronger_value():
    value = _rules("cis-rhel9")["1.5.2"]["params"]["params"][0]["value"]
    assert value == "(1|2)"


def test_rhel8_etm_check_is_automated():
    rule = _rules("cis-rhel8")["1.6.6"]
    assert rule["assessment"] == "Automated"
    assert rule["params"]["use_policy_module"] is True


def test_cloud_agent_lock_permission_is_repaired_after_boot():
    from ohbs_image._templates import HCL_LINUX_TEMPLATE

    assert "chmod 0644 /run/barad_agent.lock" in HCL_LINUX_TEMPLATE


def test_active_no_sha1_module_is_not_misread_as_backend_allow_rule(monkeypatch):
    engine = load_engine("cis-rhel9")
    monkeypatch.setattr(engine, "have", lambda _name: True)
    monkeypatch.setattr(engine, "crypto_policy_now", lambda _ctx: "DEFAULT:NO-SHA1")
    monkeypatch.setattr(
        engine, "exists",
        lambda path: path.endswith("/NO-SHA1.pmod"),
    )
    status, detail = engine.c_crypto_policy(
        SimpleNamespace(), {"kind": "no_sha1"})
    assert status == "pass"
    assert "NO-SHA1 module is active" in detail


def test_rhel8_active_no_sshetm_policy_module_passes(monkeypatch):
    engine = load_engine("cis-rhel8")
    monkeypatch.setattr(engine, "crypto_policy_now", lambda _ctx: "DEFAULT:NO-SSHETM")
    monkeypatch.setattr(
        engine, "exists",
        lambda path: path.endswith("/NO-SSHETM.pmod"),
    )
    status, detail = engine.c_crypto_policy(
        SimpleNamespace(), {"kind": "no_etm_ssh", "use_policy_module": True})
    assert status == "pass"
    assert "NO-SSHETM module is active" in detail
