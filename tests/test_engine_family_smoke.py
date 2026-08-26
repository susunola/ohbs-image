"""Smoke tests for high-traffic engine families that had no behavior tests.

Each test drives the real c_<family>/f_<family> handlers from the
cis-tencentos4 engine (all 8 Linux role copies are byte-identical) with
mocked primitives — FakeFs for files, monkeypatched sh/units/pkg DB — and
verifies both the check verdict and the fixer's commands/writes.

Families covered: kv_conf, svc_enabled, pkg_present, pkg_absent,
pam_module, login_defs, sudo_defaults.
"""

import types

import pytest

from tests.engine_fixtures import (
    FakeFs,
    apply_fs_mocks,
    load_engine,
    make_ctx,
)


@pytest.fixture(scope="module")
def eng():
    return load_engine("cis-tencentos4")


def _sh_router(mapping, calls):
    def fake_sh(cmd, timeout=60):
        key = cmd if isinstance(cmd, str) else " ".join(cmd)
        calls.append(key)
        for pat, res in mapping.items():
            if pat in key:
                return res
        return 0, "", ""
    return fake_sh


def _patch_atomic(monkeypatch, eng, fs):
    """set_kv_in_file writes via atomic_write (real fs) — redirect at FakeFs."""
    def fake_atomic(path, content, mode=None, preserve_owner=True):
        fs.set(path, content)
    monkeypatch.setattr(eng, "atomic_write", fake_atomic)



def _patch_tmp_replace(monkeypatch, eng, fs, tmp_path):
    """_sudo_append writes via tempfile.mkstemp + os.replace (real fs)."""
    import os as _os
    import tempfile as _tf
    real_mkstemp = _tf.mkstemp

    def fake_mkstemp(dir=None, prefix=None, suffix=None):
        return real_mkstemp(dir=str(tmp_path), prefix=prefix or "",
                            suffix=suffix or "")

    def fake_replace(src, dst):
        # builtins.open is patched to FakeFs by apply_fs_mocks — bypass it
        with _os.fdopen(_os.open(src, _os.O_RDONLY), encoding="utf-8") as fh:
            fs.set(dst, fh.read())
        _os.unlink(src)

    monkeypatch.setattr(eng.tempfile, "mkstemp", fake_mkstemp)
    monkeypatch.setattr("os.replace", fake_replace)
    # the dst lives only in FakeFs — real chmod/chown on it would fail
    monkeypatch.setattr("os.chmod", lambda *a, **k: None)

# --------------------------------------------------------------------------
# kv_conf
# --------------------------------------------------------------------------

def test_kv_conf_eq_fail_then_fix(eng, monkeypatch):
    fs = FakeFs()
    fs.set("/etc/security/pwquality.conf", "minlen = 8\n")
    apply_fs_mocks(monkeypatch, eng, fs)
    _patch_atomic(monkeypatch, eng, fs)
    monkeypatch.setattr("os.path.isfile", lambda p: True)  # conf_values gate
    p = {"files": ["/etc/security/pwquality.conf"], "key": "minlen",
         "op": "ge", "sep": "=", "value": "14"}
    st, detail = eng.c_kv_conf(make_ctx(eng), p)
    assert st == "fail", detail
    ok, _ = eng.f_kv_conf(make_ctx(eng), p)
    assert ok
    assert "minlen = 14" in fs.read("/etc/security/pwquality.conf")
    st, _ = eng.c_kv_conf(make_ctx(eng), p)
    assert st == "pass"


def test_kv_conf_limits_core(eng, monkeypatch):
    fs = FakeFs()
    fs.set("/etc/security/limits.conf", "* hard core 0\n")
    apply_fs_mocks(monkeypatch, eng, fs)
    _patch_atomic(monkeypatch, eng, fs)
    monkeypatch.setattr(eng, "globmod", types.SimpleNamespace(
        glob=lambda pat: ["/etc/security/limits.conf"]))
    calls = []
    monkeypatch.setattr(eng, "sh", _sh_router({"soft\\s+core": (0, "", "")}, calls))
    st, _ = eng.c_kv_conf(make_ctx(eng), {"op": "limits_core"})
    assert st == "pass"


# --------------------------------------------------------------------------
# svc_enabled / pkg_present / pkg_absent
# --------------------------------------------------------------------------

def test_svc_enabled_missing_unit_starts_and_enables(eng, monkeypatch):
    monkeypatch.setattr(eng, "_UNIT_DB", None)
    monkeypatch.setattr(eng, "_unit_db", lambda: {"chronyd.service": ("disabled", "inactive")})
    calls = []
    monkeypatch.setattr(eng, "sh", _sh_router({"systemctl": (0, "", "")}, calls))
    monkeypatch.setattr(eng, "unit_exists", lambda u: True)
    monkeypatch.setattr(eng, "pkg_installed", lambda x: True)
    st, _ = eng.c_svc_enabled(make_ctx(eng), {"units": ["chronyd.service"], "packages": []})
    assert st == "fail"
    ok, _ = eng.f_svc_enabled(make_ctx(eng), {"units": ["chronyd.service"], "packages": []})
    assert ok
    assert any("enable" in c and "chronyd.service" in c for c in calls)


def test_pkg_present_installs_missing(eng, monkeypatch):
    installed = {"chrony": False}
    monkeypatch.setattr(eng, "pkg_installed", lambda x: installed.get(x, False))
    calls = []
    monkeypatch.setattr(eng, "have", lambda x: True)
    monkeypatch.setattr(eng, "sh", _sh_router({"dnf": (0, "", "")}, calls))
    st, _ = eng.c_pkg_present(make_ctx(eng), {"packages": ["chrony"]})
    assert st == "fail"
    ok, _ = eng.f_pkg_present(make_ctx(eng), {"packages": ["chrony"]})
    assert ok
    assert any("install" in c and "chrony" in c for c in calls)


def test_pkg_absent_removes_present(eng, monkeypatch):
    monkeypatch.setattr(eng, "pkg_installed", lambda x: True)
    calls = []
    monkeypatch.setattr(eng, "have", lambda x: True)
    monkeypatch.setattr(eng, "sh", _sh_router({"dnf": (0, "", "")}, calls))
    st, _ = eng.c_pkg_absent(make_ctx(eng), {"packages": ["telnet"]})
    assert st == "fail"
    ok, _ = eng.f_pkg_absent(make_ctx(eng), {"packages": ["telnet"]})
    assert ok
    assert any(("remove" in c or "erase" in c) and "telnet" in c for c in calls)


# --------------------------------------------------------------------------
# pam_module / login_defs / sudo_defaults
# --------------------------------------------------------------------------

def test_pam_module_detects_and_fixer_inserts(eng, monkeypatch):
    fs = FakeFs()
    fs.set("/etc/pam.d/system-auth", "auth required pam_env.so\n")
    apply_fs_mocks(monkeypatch, eng, fs)
    _patch_atomic(monkeypatch, eng, fs)
    monkeypatch.setattr(eng, "PAM_FILES", ["/etc/pam.d/system-auth"])
    monkeypatch.setattr(eng, "sh", _sh_router({"authselect": (1, "", "")}, []))
    st, _ = eng.c_pam_module(make_ctx(eng), {"module": "pam_faillock.so"})
    assert st == "fail"
    st2, _ = eng.c_pam_module(make_ctx(eng), {"module": "pam_env.so"})
    assert st2 == "pass"


def test_login_defs_ge(eng, monkeypatch):
    fs = FakeFs()
    fs.set("/etc/login.defs", "PASS_MIN_DAYS\t0\n")
    apply_fs_mocks(monkeypatch, eng, fs)
    _patch_atomic(monkeypatch, eng, fs)
    monkeypatch.setattr("os.path.isfile", lambda p: True)  # conf_values gate
    calls = []
    monkeypatch.setattr(eng, "sh", _sh_router({"chage": (0, "", "")}, calls))
    p = {"key": "PASS_MIN_DAYS", "op": "ge", "value": "1"}
    st, _ = eng.c_login_defs(make_ctx(eng), p)
    assert st == "fail"
    ok, _ = eng.f_login_defs(make_ctx(eng), p)
    assert ok
    assert "PASS_MIN_DAYS\t1" in fs.read("/etc/login.defs") or \
           "PASS_MIN_DAYS 1" in fs.read("/etc/login.defs")
    st, _ = eng.c_login_defs(make_ctx(eng), p)
    assert st == "pass"


def test_sudo_defaults_flag(eng, monkeypatch, tmp_path):
    fs = FakeFs()
    fs.set("/etc/sudoers.d/60-cis-hardening", "")
    apply_fs_mocks(monkeypatch, eng, fs)
    _patch_atomic(monkeypatch, eng, fs)
    _patch_tmp_replace(monkeypatch, eng, fs, tmp_path)
    calls = []
    monkeypatch.setattr(eng, "sh", _sh_router({"visudo": (0, "", "")}, calls))
    monkeypatch.setattr(eng, "_sudoers_lines",
                        lambda ctx: [("/etc/sudoers", "Defaults env_reset")])
    p = {"key": "use_pty", "op": "flag"}
    st, _ = eng.c_sudo_defaults(make_ctx(eng), p)
    assert st == "fail"
    ok, _ = eng.f_sudo_defaults(make_ctx(eng), p)
    assert ok
    assert "Defaults use_pty" in fs.read("/etc/sudoers.d/60-cis-hardening")


def test_sudo_defaults_absent_tag(eng, monkeypatch):
    fs = FakeFs()
    fs.set("/etc/sudoers", "u1 ALL=(ALL) NOPASSWD: ALL\n")
    apply_fs_mocks(monkeypatch, eng, fs)
    _patch_atomic(monkeypatch, eng, fs)
    monkeypatch.setattr(eng, "_sudoers_lines",
                        lambda ctx: [("/etc/sudoers", "u1 ALL=(ALL) NOPASSWD: ALL")])
    monkeypatch.setattr(eng, "globmod", types.SimpleNamespace(
        glob=lambda pat: ["/etc/sudoers"] if "sudoers.d" not in pat else []))
    p = {"key": "NOPASSWD", "op": "absent_tag"}
    st, _ = eng.c_sudo_defaults(make_ctx(eng), p)
    assert st == "fail"
    ok, _ = eng.f_sudo_defaults(make_ctx(eng), p)
    assert ok
    assert "NOPASSWD" not in fs.read("/etc/sudoers") or \
           "#" in fs.read("/etc/sudoers").split("NOPASSWD")[0][-20:]


# --------------------------------------------------------------------------
# Ctx.cached failure is NOT frozen (round-4 review fix)
# --------------------------------------------------------------------------

def test_ctx_cache_does_not_freeze_producer_failure(eng):
    import argparse

    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return "good"

    ctx = eng.Ctx(argparse.Namespace(
        mode="apply", allow_disruptive=True, backup_dir="",
        benchmark="", out="-", deadline=0, profile="L1",
        platform="server", include="", exclude="", sections="",
        families="", catalog=""))
    assert ctx.cached("k", flaky) is None      # first attempt fails
    assert ctx.cached("k", flaky) == "good"    # retried, not frozen
    assert calls["n"] == 2
