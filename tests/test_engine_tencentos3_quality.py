from tests.engine_fixtures import load_engine


def test_manual_control_needs_complete_explicit_site_policy():
    engine = load_engine("cis-tencentos3")
    assert engine.c_manual(None, {})[0] == "manual"
    assert engine.c_manual(None, {"site_policy": {"approved": True}})[0] == "manual"
    status, detail = engine.c_manual(None, {"site_policy": {
        "approved": True, "reason": "approved baseline", "owner": "security",
        "reviewed_at": "2026-08-27"}})
    assert status == "pass"
    assert "owner=security" in detail


def test_journald_only_rule_is_not_applicable_when_rsyslog_is_active(monkeypatch):
    engine = load_engine("cis-tencentos3")
    monkeypatch.setattr(engine, "_unit_state", lambda _unit: ("enabled", "active"))
    status, detail = engine.c_kv_conf(None, {
        "unless_service_active": "rsyslog.service",
        "key": "ForwardToSyslog",
        "op": "eq",
    })
    assert status == "notapplicable"
    assert "rsyslog logging backend" in detail


def test_rsyslog_forward_rule_is_not_applicable_without_active_rsyslog(monkeypatch):
    engine = load_engine("cis-tencentos3")
    monkeypatch.setattr(engine, "_unit_state", lambda _unit: ("disabled", "inactive"))
    status, detail = engine.c_kv_conf(None, {
        "if_service_active": "rsyslog.service",
        "key": "ForwardToSyslog",
        "op": "eq",
    })
    assert status == "notapplicable"
    assert "alternate logging backend" in detail


def test_journal_upload_without_site_endpoint_requires_manual_evidence(monkeypatch):
    engine = load_engine("cis-tencentos3")
    monkeypatch.setattr(engine, "readlines", lambda _path: [])
    status, detail = engine.c_svc_enabled(None, {
        "units": ["systemd-journal-upload.service"],
        "packages": [],
        "requires_config": "journal-upload",
    })
    assert status == "manual"
    assert "site-specific" in detail
