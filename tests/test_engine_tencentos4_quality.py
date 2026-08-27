import threading

from tests.engine_fixtures import load_engine


def test_nftables_service_rule_is_not_applicable_with_active_firewalld(monkeypatch):
    engine = load_engine("cis-tencentos4")
    monkeypatch.setattr(
        engine,
        "_unit_state",
        lambda unit: ("enabled", "active") if unit == "firewalld.service"
        else ("disabled", "inactive"),
    )
    params = {
        "units": ["nftables.service"],
        "packages": [],
        "unless_service_active": "firewalld.service",
    }

    status, detail = engine.c_svc_enabled(None, params)

    assert status == "notapplicable"
    assert "alternate service scheme" in detail


def test_nftables_service_rule_still_applies_without_active_firewalld(monkeypatch):
    engine = load_engine("cis-tencentos4")
    monkeypatch.setattr(engine, "unit_exists", lambda _unit: True)
    monkeypatch.setattr(
        engine,
        "_unit_state",
        lambda unit: ("disabled", "inactive") if unit == "firewalld.service"
        else ("enabled", "active"),
    )
    status, _ = engine.c_svc_enabled(None, {
        "units": ["nftables.service"],
        "packages": [],
        "unless_service_active": "firewalld.service",
    })

    assert status == "pass"


def test_tencentos4_context_has_dedicated_reentrant_pam_lock():
    engine = load_engine("cis-tencentos4")
    opts = type("Opts", (), {
        "mode": "apply", "backup_dir": "", "allow_disruptive": True,
    })()

    ctx = engine.Ctx(opts)

    assert hasattr(ctx, "_pam_lock")
    assert isinstance(ctx._pam_lock, type(threading.RLock()))


def test_catalog_marks_nftables_service_as_alternative_scheme():
    import json
    from pathlib import Path

    rules = json.loads(Path(
        "ohbs_image/roles/cis-tencentos4/files/rules.json"
    ).read_text(encoding="utf-8"))
    rule = next(item for item in rules if item["id"] == "3.4.3.7")

    assert rule["params"]["unless_service_active"] == "firewalld.service"
    assert rule["params"]["unless_service_present"] == "firewalld.service"


def test_authselect_feature_accepts_effective_pam_evidence(monkeypatch, tmp_path):
    engine = load_engine("cis-tencentos4")
    pam = tmp_path / "system-auth"
    pam.write_text("auth required pam_faillock.so preauth\n", encoding="utf-8")
    monkeypatch.setattr(engine, "have", lambda name: name == "authselect")
    monkeypatch.setattr(
        engine, "sh", lambda *_args, **_kwargs:
        (0, "Profile ID: custom/cis\n", ""),
    )
    monkeypatch.setattr(engine, "_pam_paths", lambda _ctx: [str(pam)])

    status, detail = engine.c_authselect_feature(
        object(), {"feature": "with-faillock"}
    )

    assert status == "pass"
    assert "effective PAM stack" in detail


def test_authselect_feature_without_metadata_or_pam_module_still_fails(
        monkeypatch, tmp_path):
    engine = load_engine("cis-tencentos4")
    pam = tmp_path / "system-auth"
    pam.write_text("auth sufficient pam_unix.so\n", encoding="utf-8")
    monkeypatch.setattr(engine, "have", lambda name: name == "authselect")
    monkeypatch.setattr(
        engine, "sh", lambda *_args, **_kwargs:
        (0, "Profile ID: custom/cis\n", ""),
    )
    monkeypatch.setattr(engine, "_pam_paths", lambda _ctx: [str(pam)])

    status, _ = engine.c_authselect_feature(
        object(), {"feature": "with-faillock"}
    )

    assert status == "fail"
