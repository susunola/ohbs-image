"""Regression tests for the round-3 engine fixes.

Covers the bugs fixed during the 2026-08 round-3 verification builds (all
in ohbs_image/roles/*/files/ohbs_engine.py, tested against cis-tencentos4
— the 8 Linux role copies are byte-identical):

1. sshd_effective: fall back to /usr/sbin/sshd when PATH lacks sshd, and
   create /run/sshd before probing (ubuntu2404 socket-activated sshd).
2. _fix_sshd_crypto: compose the drop-in from the EFFECTIVE `sshd -T`
   algorithm lists, never the hardcoded base list (which carries aes*-cbc
   and re-introduced CBC on CBC-clean systems — round-2 5.1.6 regression).
3. c_svc_disabled/f_svc_disabled: the "provider package not installed"
   short-circuit must not excuse units that DO exist (ubuntu2004 4.2.2:
   nftables package absent hid an enabled firewalld).
4. c_ufw_rules: ufw installed but inactive is a FAIL (not notapplicable);
   f_ufw_rules self-enables ufw under the shared command lock.
5. c_updates_applied (apt): pending updates that are all dpkg-held pass
   (vendor pins cannot be upgraded); any non-held pending update fails.
6. f_pam_arg: recognise and strip authselect template macros such as
   `{if not "without-nullok":nullok}` when removing an argument.
7. f_bootloader_password (Debian path): normalise a bare nonzero numeric
   GRUB_DEFAULT to 0 (GRUB refuses to auto-boot a submenu default once a
   superuser is defined); GRUB_DEFAULT=0 / saved stay untouched.

Mocking follows tests/test_engine_new_families.py: engine primitives
(eng.sh, eng._unit_db, eng.have, eng.globmod, ...) are monkeypatched and
files live in FakeFs (basename-keyed).
"""

import fnmatch
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


def _mock_units(monkeypatch, eng, db):
    """Patch the engine's unit snapshot: {unit: (enabled_state, active_state)}."""
    monkeypatch.setattr(eng, "_UNIT_DB", None)
    monkeypatch.setattr(eng, "_unit_db", lambda: dict(db))


def _mock_glob(monkeypatch, eng, paths):
    """Patch eng.globmod.glob to match against the given full paths."""
    paths = sorted(paths)

    def fake_glob(pat):
        return [p for p in paths if fnmatch.fnmatchcase(p, pat)]

    monkeypatch.setattr(eng, "globmod", types.SimpleNamespace(glob=fake_glob))


def _sh_router(mapping, calls=None):
    """Fake sh() from {cmd-as-tuple-or-str: (rc, out, err)}; None = default."""
    def fake_sh(cmd, timeout=60):
        if calls is not None:
            calls.append(cmd)
        key = cmd if isinstance(cmd, str) else tuple(cmd)
        return mapping.get(key, mapping.get(None, (0, "", "")))
    return fake_sh


class _RecordingLock:
    """file_lock replacement that brackets its body in a shared event log."""

    def __init__(self, events, path):
        self._events = events
        self._path = path

    def __enter__(self):
        self._events.append(("enter", self._path))
        return self

    def __exit__(self, *exc):
        self._events.append(("exit", self._path))
        return False


# -- 1. sshd_effective --------------------------------------------------------

_SSHD_T = (
    "port 22\n"
    "ciphers chacha20-poly1305@openssh.com,aes128-ctr,aes192-ctr,aes256-ctr,"
    "aes128-gcm@openssh.com,aes256-gcm@openssh.com\n"
    "macs hmac-sha2-256-etm@openssh.com,hmac-sha2-512-etm@openssh.com,"
    "hmac-sha2-256,hmac-sha2-512\n"
)


def test_sshd_effective_falls_back_to_usr_sbin(eng, monkeypatch):
    """PATH without sshd -> probe /usr/sbin/sshd; /run/sshd is created."""
    calls = []
    made_dirs = []
    monkeypatch.setattr(eng, "shutil",
                        types.SimpleNamespace(which=lambda b: None))
    fake_os = types.SimpleNamespace(
        path=types.SimpleNamespace(exists=lambda p: p == "/usr/sbin/sshd"),
        makedirs=lambda p, exist_ok=False: made_dirs.append(p),
    )
    monkeypatch.setattr(eng, "os", fake_os)
    monkeypatch.setattr(eng, "sh", _sh_router(
        {("/usr/sbin/sshd", "-T"): (0, _SSHD_T, "")}, calls))
    ctx = make_ctx(eng)
    d = eng.sshd_effective(ctx)
    assert calls and calls[0] == ["/usr/sbin/sshd", "-T"], calls
    assert "/run/sshd" in made_dirs
    assert d["ciphers"]  # parsed output, not an error/empty dict
    assert "aes128-cbc" not in d["ciphers"][0]


# -- 2. _fix_sshd_crypto composes from the effective config -------------------

def test_sshd_crypto_fix_does_not_reintroduce_cbc(eng, monkeypatch):
    """Drop-in absent: trim from `sshd -T` lists, not _SSH_BASE_CIPHERS."""
    # Sanity: the hardcoded base list really carries CBC (the regression
    # source), while the mocked effective config does not.
    assert any(c.endswith("-cbc") for c in eng._SSH_BASE_CIPHERS)

    fakefs = FakeFs()
    fakefs.set("/etc/ssh/sshd_config",
               "Include /etc/ssh/sshd_config.d/*.conf\nPermitRootLogin no\n")
    apply_fs_mocks(monkeypatch, eng, fakefs)
    monkeypatch.setattr(eng, "shutil",
                        types.SimpleNamespace(which=lambda b: "/usr/sbin/sshd"))
    fake_os = types.SimpleNamespace(
        path=types.SimpleNamespace(exists=lambda p: False),
        makedirs=lambda *a, **k: None,
        unlink=lambda p: None,
    )
    monkeypatch.setattr(eng, "os", fake_os)

    def fake_sh(cmd, timeout=60):
        if cmd == ["/usr/sbin/sshd", "-T"]:
            return 0, _SSHD_T, ""
        if isinstance(cmd, str) and cmd.startswith("command -v sshd"):
            return 0, "/usr/sbin/sshd", ""
        if cmd == ["/usr/sbin/sshd", "-t"]:
            return 0, "", ""
        return 0, "", ""

    monkeypatch.setattr(eng, "sh", fake_sh)
    ctx = make_ctx(eng)
    ok, msg = eng.f_crypto_policy(ctx, {"kind": "no_weak_mac"})
    assert ok, msg
    body = fakefs.read(eng.SSH_CRYPTO_DROPIN)
    assert body, "drop-in was not written"
    ciphers_ln = next(ln for ln in body.splitlines()
                      if ln.startswith("Ciphers "))
    assert "-cbc" not in ciphers_ln, ciphers_ln
    # The list came from the effective config, not the base list.
    assert "aes128-ctr" in ciphers_ln
    macs_ln = next(ln for ln in body.splitlines() if ln.startswith("MACs "))
    assert "hmac-sha2-256" in macs_ln
    assert "sshd" in ctx.restarts


# -- 3. svc_disabled: package short-circuit must not excuse existing units ----

def test_svc_disabled_fails_for_existing_enabled_unit(eng, monkeypatch):
    """packages all absent, but an existing enabled/active unit must fail
    (ubuntu2004 4.2.2: nftables pkg absent hid an enabled firewalld)."""
    _mock_units(monkeypatch, eng, {"firewalld.service": ("enabled", "active")})
    monkeypatch.setattr(eng, "pkg_installed", lambda n: False)
    ctx = make_ctx(eng)
    st, detail = eng.c_svc_disabled(ctx, {
        "units": ["nftables.service", "firewalld.service"],
        "packages": ["nftables", "firewalld"],
    })
    assert st == "fail", detail
    assert "firewalld.service" in detail


def test_svc_disabled_package_shortcircuit_only_when_no_units(eng, monkeypatch):
    """No unit exists and no package installed -> still a vacuous pass."""
    _mock_units(monkeypatch, eng, {})
    monkeypatch.setattr(eng, "pkg_installed", lambda n: False)
    ctx = make_ctx(eng)
    st, detail = eng.c_svc_disabled(ctx, {
        "units": ["nftables.service"],
        "packages": ["nftables"],
    })
    assert st == "pass", detail
    assert "not installed" in detail


def test_svc_disabled_fixer_stops_disables_masks(eng, monkeypatch):
    _mock_units(monkeypatch, eng, {"firewalld.service": ("enabled", "active")})
    monkeypatch.setattr(eng, "pkg_installed", lambda n: False)
    calls = []
    monkeypatch.setattr(eng, "sh", _sh_router({}, calls))
    ctx = make_ctx(eng)
    ok, msg = eng.f_svc_disabled(ctx, {
        "units": ["nftables.service", "firewalld.service"],
        "packages": ["nftables", "firewalld"],
    })
    assert ok, msg
    assert ["systemctl", "stop", "firewalld.service"] in calls
    assert ["systemctl", "--now", "disable", "firewalld.service"] in calls
    assert ["systemctl", "mask", "firewalld.service"] in calls
    # The absent unit must not be touched.
    assert not any("nftables.service" in c for c in calls if isinstance(c, list))


# -- 4. ufw_rules: installed-but-inactive is fail; fixer self-enables ----------

def _mock_ufw_inactive(monkeypatch, eng, have_ufw, events=None):
    _mock_units(monkeypatch, eng, {})  # ufw.service absent/not in use

    def fake_sh(cmd, timeout=60):
        if events is not None:
            events.append(("sh", cmd))
        if cmd == ["ufw", "status"]:
            return 0, "Status: inactive", ""
        return 0, "", ""

    monkeypatch.setattr(eng, "sh", fake_sh)
    monkeypatch.setattr(eng, "have", lambda b: have_ufw)


def test_ufw_rules_fail_when_installed_but_inactive(eng, monkeypatch):
    _mock_ufw_inactive(monkeypatch, eng, have_ufw=True)
    ctx = make_ctx(eng)
    st, detail = eng.c_ufw_rules(ctx, {"kind": "loopback"})
    assert st == "fail", detail
    assert "not active" in detail


def test_ufw_rules_notapplicable_when_binary_missing(eng, monkeypatch):
    _mock_ufw_inactive(monkeypatch, eng, have_ufw=False)
    ctx = make_ctx(eng)
    st, detail = eng.c_ufw_rules(ctx, {"kind": "loopback"})
    assert st == "notapplicable", detail


def test_ufw_rules_fixer_enables_ufw_under_lock(eng, monkeypatch):
    events = []
    _mock_ufw_inactive(monkeypatch, eng, have_ufw=True, events=events)
    fakefs = FakeFs()
    apply_fs_mocks(monkeypatch, eng, fakefs)
    ctx = make_ctx(eng)
    ctx.file_lock = lambda path: _RecordingLock(events, path)
    ok, msg = eng.f_ufw_rules(ctx, {"kind": "loopback"})
    assert ok, msg
    sh_cmds = [c for kind, c in events if kind == "sh"]
    # Self-enable is the LAST ufw command.
    assert sh_cmds[-1] == ["ufw", "--force", "enable"], sh_cmds
    # The SSH whitelist goes in before any default-deny/enable.
    assert ["ufw", "allow", "22/tcp"] in sh_cmds
    # Every rule command + enable runs inside the shared ufw command lock.
    enter = events.index(("enter", "__cmd__:ufw"))
    exit_ = events.index(("exit", "__cmd__:ufw"))
    inside = [c for kind, c in events[enter:exit_] if kind == "sh"]
    assert ["ufw", "allow", "in", "on", "lo"] in inside
    assert ["ufw", "deny", "in", "from", "127.0.0.0/8"] in inside
    assert ["ufw", "--force", "enable"] in inside
    # No ufw rule/enable command outside the lock (status probes and the
    # SSH whitelist are allowed to precede it).
    outside = [c for kind, c in (events[:enter] + events[exit_:])
               if kind == "sh" and isinstance(c, list)
               and c[:1] == ["ufw"] and c[1:2] != ["status"]]
    assert outside == [["ufw", "allow", "22/tcp"]], outside


# -- 5. c_updates_applied (apt): dpkg-held pending updates --------------------

_APT_PENDING = (
    "Listing...\n"
    "cloud-init/jammy-updates 25.1 amd64 [upgradable from: 25.0]\n"
    "vim/jammy-updates 2:9.0 amd64 [upgradable from: 2:8.2]\n"
)


def _apt_checker(monkeypatch, eng, dpkg_hold_output):
    monkeypatch.setattr(eng, "have", lambda b: b == "apt-get")

    def fake_sh(cmd, timeout=60):
        if cmd == ["apt", "list", "--upgradable"]:
            return 0, _APT_PENDING, ""
        if isinstance(cmd, str) and cmd.startswith("dpkg --get-selections"):
            return 0, dpkg_hold_output, ""
        return 0, "", ""

    monkeypatch.setattr(eng, "sh", fake_sh)


def test_updates_applied_apt_all_held_passes(eng, monkeypatch):
    _apt_checker(monkeypatch, eng, "cloud-init\thold\nvim\thold\n")
    ctx = make_ctx(eng)
    st, detail = eng.c_updates_applied(ctx, {})
    assert st == "pass", detail
    assert "held" in detail


def test_updates_applied_apt_unheld_pending_fails(eng, monkeypatch):
    _apt_checker(monkeypatch, eng, "cloud-init\thold\n")
    ctx = make_ctx(eng)
    st, detail = eng.c_updates_applied(ctx, {})
    assert st == "fail", detail
    assert "vim" in detail
    assert "cloud-init" not in detail


# -- 6. f_pam_arg strips authselect template macros ----------------------------

_SYSTEM_AUTH_MACRO = (
    "auth     required    pam_env.so\n"
    'auth     sufficient  pam_unix.so {if not "without-nullok":nullok}'
    " try_first_pass\n"
    "password sufficient  pam_unix.so sha512 shadow nullok\n"
    "account  required    pam_unix.so\n"
)


def test_pam_arg_absent_strips_authselect_macro(eng, monkeypatch):
    fakefs = FakeFs()
    fakefs.set("/etc/pam.d/system-auth", _SYSTEM_AUTH_MACRO)
    apply_fs_mocks(monkeypatch, eng, fakefs)
    monkeypatch.setattr(eng, "have", lambda b: False)  # no authselect
    monkeypatch.setattr(eng, "sh", _sh_router(
        {("authselect", "current"): (1, "", "")}))
    ctx = make_ctx(eng)
    p = {"module": "pam_unix.so", "arg": "nullok", "mode": "absent"}
    # Plain `nullok` on the password line makes the checker fail first.
    st, detail = eng.c_pam_arg(ctx, p)
    assert st == "fail", detail
    ok, msg = eng.f_pam_arg(ctx, p)
    assert ok, msg
    content = fakefs.read("/etc/pam.d/system-auth")
    assert "nullok" not in content, content  # macro AND plain arg gone
    assert "pam_unix.so" in content  # module lines themselves survive
    st, detail = eng.c_pam_arg(ctx, p)
    assert st == "pass", detail


# -- 7. f_bootloader_password (Debian): GRUB_DEFAULT normalisation -------------

_GRUB_D_10_LINUX = (
    '#!/bin/sh\n'
    'CLASS="--class gnu-linux --class gnu --class os"\n'
)
_GRUB_CFG = (
    "menuentry 'Ubuntu, with Linux 6.8.0-71-generic' --unrestricted "
    "--class ubuntu --class gnu-linux {\n"
    "\tlinux /vmlinuz root=/dev/vda3\n"
    "}\n"
)


def _bootloader_fs():
    fakefs = FakeFs()
    fakefs.set("/etc/grub.d/10_linux", _GRUB_D_10_LINUX)
    fakefs.set("/boot/grub/grub.cfg", _GRUB_CFG)
    return fakefs


def _run_bootloader_fix(monkeypatch, eng, fakefs, grub_default_txt):
    fakefs.set("/etc/default/grub", grub_default_txt)
    apply_fs_mocks(monkeypatch, eng, fakefs)
    monkeypatch.setattr(eng, "have", lambda b: False)  # no grub2-mkconfig
    _mock_glob(monkeypatch, eng, ["/etc/grub.d/10_linux"])
    calls = []
    monkeypatch.setattr(eng, "sh", _sh_router({}, calls))
    ctx = make_ctx(eng)
    ok, msg = eng.f_bootloader_password(ctx, {})
    assert ok, msg
    assert ["update-grub"] in calls
    return ctx


def test_bootloader_password_rewrites_nonzero_grub_default(eng, monkeypatch):
    fakefs = _bootloader_fs()
    _run_bootloader_fix(monkeypatch, eng, fakefs,
                        "GRUB_DEFAULT=1\nGRUB_TIMEOUT=5\n")
    txt = fakefs.read("/etc/default/grub")
    assert "GRUB_DEFAULT=0" in txt, txt
    assert "GRUB_DEFAULT=1" not in txt, txt
    # The 10_linux generator got --unrestricted and 01_users was written.
    assert "--unrestricted" in fakefs.read("/etc/grub.d/10_linux")
    assert 'set superusers="root"' in fakefs.read("/etc/grub.d/01_users")
    assert fakefs.read("/root/ohbs-image-grub-password")


@pytest.mark.parametrize("default_line", ["GRUB_DEFAULT=0", "GRUB_DEFAULT=saved"])
def test_bootloader_password_keeps_zero_and_saved_default(eng, monkeypatch,
                                                          default_line):
    fakefs = _bootloader_fs()
    original = f"{default_line}\nGRUB_TIMEOUT=5\n"
    _run_bootloader_fix(monkeypatch, eng, fakefs, original)
    assert fakefs.read("/etc/default/grub") == original
    assert not any(path == "/etc/default/grub"
                   for path, _ in fakefs.written)
