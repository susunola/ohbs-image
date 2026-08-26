"""Tests for scripts/check_readme.py — the CI README-freshness guard."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import check_readme  # noqa: E402

import ohbs_image  # noqa: E402

ALL_CMDS = {"audit", "build", "catalog", "check-source", "clean", "cleanup", "cleanup-images", "cleanup-runs", "config", "configure", "discover", "doctor", "drift", "engine", "launch", "plan", "promote", "report", "rollback", "run", "state", "verify-release",
            "images", "init", "list", "pending", "preflight", "quickstart", "scan", "test",
            "try", "validate", "verify", "verify-image"}


class TestReadmeDocumentedSubcommands:
    def test_all_present(self):
        readme = "\n".join(f"ohbs-image {c}" for c in ALL_CMDS)
        found = check_readme.readme_documented_subcommands(readme, ALL_CMDS)
        assert found == ALL_CMDS

    def test_verify_image_not_swallowed_by_verify(self):
        """`verify-image` must not be reported present just because `verify`
        appears; and `verify` must not be over-matched by `verify-image`."""
        readme = "ohbs-image verify\nohbs-image verify-image\n"
        found = check_readme.readme_documented_subcommands(readme, ALL_CMDS)
        assert "verify" in found
        assert "verify-image" in found

    def test_missing_command_reported(self):
        readme = "ohbs-image build\nohbs-image init\n"
        found = check_readme.readme_documented_subcommands(readme, ALL_CMDS)
        assert "build" in found and "init" in found
        missing = ALL_CMDS - found
        assert "audit" in missing and "verify-image" in missing

    def test_inline_flag_does_not_fake_presence(self):
        """A bare word like `build` inside prose (not `ohbs-image build`) must
        not count as documenting the command."""
        readme = "the build step runs packer\nohbs-image init\n"
        found = check_readme.readme_documented_subcommands(readme, ALL_CMDS)
        assert "build" not in found  # only `ohbs-image init` is authoritative
        assert "init" in found


class TestReadmeDocumentedProfiles:
    def test_all_profiles_present(self):
        readme = " ".join(check_readme._PROFILE_NAMES)
        found = check_readme.readme_documented_profiles(readme)
        assert found == set(check_readme._PROFILE_NAMES)

    def test_missing_profile_reported(self):
        readme = "ubuntu2004 rhel9 win2022"
        found = check_readme.readme_documented_profiles(readme)
        assert "tencentos4" not in found


class TestCheckReadme:
    def test_returns_empty_when_current(self):
        readme = "\n".join(f"ohbs-image {c}" for c in ALL_CMDS) + "\n" + \
                 " ".join(check_readme._PROFILE_NAMES)
        assert check_readme.check_readme(readme, ALL_CMDS,
                                         set(check_readme._PROFILE_NAMES)) == []

    def test_reports_missing_command(self):
        readme = "ohbs-image init"
        errors = check_readme.check_readme(readme, ALL_CMDS,
                                           set(check_readme._PROFILE_NAMES))
        assert any("subcommand" in e for e in errors)
        assert "build" in errors[0]

    def test_reports_missing_profile(self):
        readme = "ohbs-image init\nubuntu2004 rhel9"
        errors = check_readme.check_readme(
            readme, {"init"}, {"ubuntu2004", "rhel9", "tencentos4"})
        assert any("profile" in e for e in errors)
        assert "tencentos4" in [e for e in errors if "profile" in e][0]

    def test_reports_stale_version_badge(self):
        readme = ("ohbs-image init\nubuntu2004 rhel9\n"
                  "https://img.shields.io/badge/version-0.12.4-blue")
        errors = check_readme.check_readme(readme, {"init"},
                                           {"ubuntu2004", "rhel9"}, "0.17.0")
        assert any("version badge" in e for e in errors)


def monkeypatch_open(content: str):
    """Point check_readme's Path.read_text at an in-memory string and make any
    path look like it exists (so main() proceeds past the existence check)."""
    import unittest.mock as mock
    def fake_read_text(self, *a, **kw):  # noqa: ANN001
        return content
    return mock.patch.multiple(
        Path,
        read_text=fake_read_text,
        exists=lambda self: True,
    )


class TestMainExitCodes:
    def test_missing_readme_returns_1(self):
        assert check_readme.main(["--readme", "/nonexistent/README.md"]) == 1

    def test_main_returns_1_when_out_of_date(self, monkeypatch, capsys):
        """main() exits 1 (not 0) and prints the missing items when the
        README is missing a command."""
        readme = "ohbs-image init"
        monkeypatch.setattr(check_readme, "registered_subcommands",
                            lambda: set(ALL_CMDS))
        with monkeypatch_open(readme):
            rc = check_readme.main(["--readme", "/tmp/fake_readme.md"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "build" in err

    def test_main_returns_0_when_current(self, monkeypatch):
        readme = "\n".join(f"ohbs-image {c}" for c in ALL_CMDS) + "\n" + \
                 " ".join(check_readme._PROFILE_NAMES) + \
                 f"\nhttps://img.shields.io/badge/version-{ohbs_image.VERSION}-blue"
        monkeypatch.setattr(check_readme, "registered_subcommands",
                            lambda: set(ALL_CMDS))
        monkeypatch.setattr(check_readme, "check_profile_count_in_packaging",
                            lambda: [])
        with monkeypatch_open(readme):
            rc = check_readme.main(["--readme", "/tmp/fake_readme.md"])
        assert rc == 0


class TestCheckProfileCountInPackaging:
    """The PyPI description and package docstring must agree with the live
    profile count (guards "12 OS profiles" drift after a profile is added)."""

    def _write_repo(self, tmp_path, count: int):
        (tmp_path / "pyproject.toml").write_text(
            f'description = "... CLI, {count} OS profiles, ..."\n',
            encoding="utf-8")
        (tmp_path / "ohbs_image").mkdir()
        (tmp_path / "ohbs_image" / "__init__.py").write_text(
            '"""\nSupported OS: Ubuntu 20/22/24, RHEL 8/9/10, Rocky 9,\n'
            '              TencentOS 3/4, Windows Server '
            '2016/2019/2022/2025\n"""\n',
            encoding="utf-8")

    def test_clean_state_returns_empty(self, tmp_path, monkeypatch):
        import ohbs_image
        monkeypatch.setattr(check_readme, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(ohbs_image, "PROFILES",
                            dict.fromkeys(check_readme._PROFILE_NAMES))
        self._write_repo(tmp_path, len(check_readme._PROFILE_NAMES))
        assert check_readme.check_profile_count_in_packaging() == []

    def test_stale_pyproject_count_reported(self, tmp_path, monkeypatch):
        import ohbs_image
        monkeypatch.setattr(check_readme, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(ohbs_image, "PROFILES",
                            dict.fromkeys(check_readme._PROFILE_NAMES))
        self._write_repo(tmp_path, 12)
        errors = check_readme.check_profile_count_in_packaging()
        assert any("pyproject.toml" in e for e in errors)

    def test_stale_init_os_list_reported(self, tmp_path, monkeypatch):
        import ohbs_image
        monkeypatch.setattr(check_readme, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(ohbs_image, "PROFILES",
                            dict.fromkeys(check_readme._PROFILE_NAMES))
        self._write_repo(tmp_path, len(check_readme._PROFILE_NAMES))
        init = tmp_path / "ohbs_image" / "__init__.py"
        init.write_text('"""\nSupported OS: Ubuntu 20/22/24\n"""\n',
                        encoding="utf-8")
        errors = check_readme.check_profile_count_in_packaging()
        assert any("__init__.py" in e for e in errors)


class TestCheckTestConsistency:
    """Tests for check_readme.check_test_consistency()."""

    def test_current_state_is_consistent(self):
        """The committed tests/test_check_readme.py matches the live CLI."""
        import ohbs_image
        registered = set(ohbs_image.build_parser()._subparsers._group_actions[0].choices)
        profiles = set(ohbs_image.PROFILES.keys())
        assert check_readme.check_test_consistency(registered, profiles) == []

    def test_reports_missing_command_in_all_cmds(self, tmp_path, monkeypatch):
        """If ALL_CMDS drops a command the CLI registers, it's reported."""
        src = (check_readme.REPO_ROOT / "tests" / "test_check_readme.py").read_text()
        # simulate ALL_CMDS missing 'audit' by stripping one entry
        src = src.replace('"audit", ', '')
        (tmp_path / "tests").mkdir()
        f = tmp_path / "tests" / "test_check_readme.py"
        f.write_text(src)
        monkeypatch.setattr(check_readme, "REPO_ROOT", tmp_path)
        errs = check_readme.check_test_consistency({"audit", "build"}, set())
        assert any("audit" in e and "ALL_CMDS" in e for e in errs)

    def test_reports_unknown_command_in_all_cmds(self, tmp_path, monkeypatch):
        """If ALL_CMDS lists a command the CLI doesn't register, it's reported."""
        src = (check_readme.REPO_ROOT / "tests" / "test_check_readme.py").read_text()
        src = src.replace("}",
            '"phantom-cmd"}', 1)
        (tmp_path / "tests").mkdir()
        f = tmp_path / "tests" / "test_check_readme.py"
        f.write_text(src)
        monkeypatch.setattr(check_readme, "REPO_ROOT", tmp_path)
        errs = check_readme.check_test_consistency({"audit"}, set())
        assert any("phantom-cmd" in e and "unknown" in e for e in errs)

    def test_reports_missing_test_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(check_readme, "REPO_ROOT", tmp_path)
        errs = check_readme.check_test_consistency({"audit"}, set())
        assert any("test file not found" in e for e in errs)


class TestTranslationStaleCommands:
    """Tests for check_readme.translation_stale_commands() — translations are
    condensed by design, so only *stale* (renamed/removed) command references
    are treated as errors, not missing coverage."""

    def test_condensed_translation_with_real_commands_passes(self):
        text = "`ohbs-image init` と `ohbs-image build` のみ"
        assert check_readme.translation_stale_commands(
            text, {"init", "build"}) == []

    def test_stale_command_reported(self):
        text = "`ohbs-image init` and a stale `ohbs-image obsoleted-cmd`"
        assert check_readme.translation_stale_commands(
            text, {"init"}) == ["obsoleted-cmd"]

    def test_backtick_required(self):
        """Prose such as "ohbs-image launches a CVM" (no backticks) must not
        be misread as a command reference."""
        text = "ohbs-image launches a CVM"
        assert check_readme.translation_stale_commands(text, {"init"}) == []

    def test_flags_do_not_fake_a_command(self):
        text = "`ohbs-image scan --xccdf out.xml` and `ohbs-image build -y`"
        assert check_readme.translation_stale_commands(
            text, {"scan", "build"}) == []


class TestMainCheckTranslations:
    def test_main_check_translations_reports_stale(self, monkeypatch,
                                                   tmp_path, capsys):
        """main() with --check-translations exits 1 and names the stale
        command when a translation references a command that no longer
        exists in the CLI."""
        monkeypatch.setattr(check_readme, "registered_subcommands",
                            lambda: {"init", "build"})
        profiles = " ".join(check_readme._PROFILE_NAMES)
        (tmp_path / "README.md").write_text(
            f"ohbs-image init\nohbs-image build\n{profiles}\n"
            f"https://img.shields.io/badge/version-{ohbs_image.VERSION}-blue\n",
            encoding="utf-8")
        for name in ("README.zh-CN.md", "README.ja.md", "README.th.md"):
            (tmp_path / name).write_text(
                "`ohbs-image init` and a stale `ohbs-image ghost-cmd`\n",
                encoding="utf-8")
        monkeypatch.setattr(check_readme, "REPO_ROOT", tmp_path)
        (tmp_path / "pyproject.toml").write_text(
            'description = "13 OS profiles"\n', encoding="utf-8")
        (tmp_path / "ohbs_image").mkdir()
        (tmp_path / "ohbs_image" / "__init__.py").write_text(
            '"""\nSupported OS: Ubuntu 20/22/24, RHEL 8/9/10, Rocky 9,\n'
            '              TencentOS 3/4, Windows Server '
            '2016/2019/2022/2025\n"""\n',
            encoding="utf-8")
        rc = check_readme.main(["--readme", str(tmp_path / "README.md"),
                                "--check-translations"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "ghost-cmd" in err

    def test_main_check_translations_passes_when_clean(self, monkeypatch,
                                                       tmp_path):
        """--check-translations passes when every referenced command exists
        (translations do NOT need full subcommand coverage)."""
        monkeypatch.setattr(check_readme, "registered_subcommands",
                            lambda: {"init", "build"})
        profiles = " ".join(check_readme._PROFILE_NAMES)
        (tmp_path / "README.md").write_text(
            f"ohbs-image init\nohbs-image build\n{profiles}\n"
            f"https://img.shields.io/badge/version-{ohbs_image.VERSION}-blue\n",
            encoding="utf-8")
        for name in ("README.zh-CN.md", "README.ja.md", "README.th.md"):
            # condensed quick-start style: only two commands documented
            (tmp_path / name).write_text(
                "`ohbs-image init` and `ohbs-image build`\n", encoding="utf-8")
        monkeypatch.setattr(check_readme, "REPO_ROOT", tmp_path)
        (tmp_path / "pyproject.toml").write_text(
            'description = "13 OS profiles"\n', encoding="utf-8")
        (tmp_path / "ohbs_image").mkdir()
        (tmp_path / "ohbs_image" / "__init__.py").write_text(
            '"""\nSupported OS: Ubuntu 20/22/24, RHEL 8/9/10, Rocky 9,\n'
            '              TencentOS 3/4, Windows Server '
            '2016/2019/2022/2025\n"""\n',
            encoding="utf-8")
        rc = check_readme.main(["--readme", str(tmp_path / "README.md"),
                                "--check-translations"])
        assert rc == 0
