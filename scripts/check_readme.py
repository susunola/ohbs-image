#!/usr/bin/env python3
"""Verify README.md stays in sync with the codebase's CLI surface.

Ensures every subcommand registered by ``ohbs_image.build_parser()`` and every
OS profile in ``ohbs_image.PROFILES`` is documented in README.md (as
``ohbs-image <cmd>`` / the profile name).  Run in CI so adding or removing a
command or profile without updating the docs fails the build.

With ``--check-translations``, the translated READMEs (zh-CN / ja / th) are
held to a lighter, deliberate contract: they are condensed quick-start
documents, so they are NOT required to cover every subcommand — but every
``ohbs-image <cmd>`` they do reference must still exist in the CLI (no stale
references after a rename or removal).

Usage:
    python3 scripts/check_readme.py [--readme PATH] [--check-tests]
                                    [--check-translations]

The check is pure stdlib + the installed ``ohbs_image`` package (already
importable in the CI test job, where this runs after ``pip install -e .``).
Exit code 0 = docs current; 1 = something is missing.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from _cli_introspection import registered_subcommands as _cli_registered_subcommands

REPO_ROOT = Path(__file__).resolve().parent.parent

# Must stay in sync with the profiles dict in ohbs_image/_profiles.py — the
# check uses this to find profile names in README without importing the full
# module at parse time (kept as a flat alternation for the regex).
_PROFILE_NAMES = (
    "ubuntu2004", "ubuntu2204", "ubuntu2404",
    "rhel8", "rhel9", "rhel10", "rocky9",
    "tencentos3", "tencentos4",
    "win2016", "win2019", "win2022", "win2025",
)


def registered_subcommands() -> set[str]:
    """Return the set of subcommand names the CLI actually registers."""
    import ohbs_image
    return _cli_registered_subcommands(ohbs_image.build_parser())


def readme_documented_subcommands(readme_text: str,
                                  registered: set[str]) -> set[str]:
    """Which of *registered* subcommands appear in *readme_text*."""
    found: set[str] = set()
    # Longest-first so `verify` doesn't swallow `verify-image`.
    ordered = sorted(registered, key=len, reverse=True)
    for m in re.finditer(r"ohbs-image\s+([a-z][a-z-]*)", readme_text):
        word = m.group(1)
        for cmd in ordered:
            if word == cmd or word.startswith(cmd + "-"):
                found.add(cmd)
                break
    return found


def readme_documented_profiles(readme_text: str) -> set[str]:
    """Which profile names appear in *readme_text*."""
    pattern = r"\b(?:" + "|".join(re.escape(p) for p in _PROFILE_NAMES) + r")\b"
    return set(re.findall(pattern, readme_text))


def translation_stale_commands(translation_text: str,
                               registered: set[str]) -> list[str]:
    """Which *backticked* ``ohbs-image <cmd>`` references in a translation no
    longer exist in the CLI.

    Translations are condensed quick-start documents and are NOT required to
    enumerate every subcommand (unlike README.md). What they must never do is
    reference a command that was renamed or removed — a stale reference is the
    actual rot mode. Backticks are required so prose such as "ohbs-image
    launches a CVM" is not misread as a command reference.
    """
    stale: set[str] = set()
    for m in re.finditer(r"`ohbs-image\s+([a-z][a-z-]*)", translation_text):
        word = m.group(1)
        if word not in registered:
            stale.add(word)
    return sorted(stale)


def check_readme(readme_text: str, registered: set[str],
                 profiles: set[str], version: str = "") -> list[str]:
    """Return a list of human-readable problems, empty when docs are current."""
    errors: list[str] = []

    doc_cmds = readme_documented_subcommands(readme_text, registered)
    missing_cmds = sorted(registered - doc_cmds)
    if missing_cmds:
        errors.append("README.md does not document these subcommand(s): "
                      + ", ".join(missing_cmds))

    doc_profiles = readme_documented_profiles(readme_text)
    missing_profiles = sorted(profiles - doc_profiles)
    if missing_profiles:
        errors.append("README.md does not mention these OS profile(s): "
                      + ", ".join(missing_profiles))

    if version:
        # The prominent version badge is a release promise, not an arbitrary
        # historical example. Keep it tied to the package's single source of
        # truth so users do not install a version different from the one the
        # README advertises.
        badge = re.search(r"shields\.io/badge/version-([^?-]+)-blue", readme_text)
        if badge is None:
            errors.append("README.md is missing its version badge")
        elif badge.group(1) != version:
            errors.append("README.md version badge is " + badge.group(1)
                          + ", but package VERSION is " + version)

    return errors


_PROFILE_COUNT_RE = re.compile(r"(\d+)\s+OS profiles?\b")
# "Supported OS: Ubuntu 20/22/24, RHEL 8/9/10, Rocky 9, TencentOS 3/4, ..."
# — each family contributes one profile per slash-separated version.
_INIT_OS_RE = re.compile(
    r"Supported OS:\s*((?:[A-Za-z][A-Za-z0-9 -]*?\s+\d[\d/ ]*,?\s*)+)")


def check_profile_count_in_packaging() -> list[str]:
    """The profile count appears in places a user actually sees (the PyPI
    project description and the package docstring) and can silently drift
    when a profile is added — e.g. "12 OS profiles" after rocky9 landed.
    Guard both against the live PROFILES dict so a profile add/remove
    without updating them fails CI the same way README drift does.
    """
    import ohbs_image

    errors: list[str] = []
    expected = len(ohbs_image.PROFILES)

    pyproject = REPO_ROOT / "pyproject.toml"
    if pyproject.exists():
        m = _PROFILE_COUNT_RE.search(pyproject.read_text(encoding="utf-8"))
        if m is None or int(m.group(1)) != expected:
            errors.append(f"pyproject.toml description should say "
                          f"{expected} OS profiles")

    init_text = (REPO_ROOT / "ohbs_image" / "__init__.py").read_text(
        encoding="utf-8")
    m = _INIT_OS_RE.search(init_text)
    if m is None:
        errors.append("ohbs_image/__init__.py docstring has no "
                      "Supported OS list")
        return errors
    listed = sum(len(ver.split("/")) for ver in m.group(1).split(","))
    if listed != expected:
        errors.append(f"ohbs_image/__init__.py Supported OS list should cover "
                      f"{expected} profiles (found {listed})")
    return errors


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--readme", default=str(REPO_ROOT / "README.md"),
                   help="Path to README.md (default: repo README)")
    p.add_argument("--check-tests", action="store_true",
                   help="Also verify tests/test_check_readme.py's hardcoded "
                        "command/profile lists match the live CLI (default: off)")
    p.add_argument("--check-translations", action="store_true",
                   help="Also verify the translated READMEs (zh-CN / ja / th) "
                        "do not reference any command that no longer exists "
                        "in the CLI, so a renamed/removed command cannot "
                        "leave a stale reference in another language "
                        "(default: off)")
    args = p.parse_args(argv)

    readme_path = Path(args.readme)
    if not readme_path.exists():
        print(f"check_readme: README not found: {readme_path}", file=sys.stderr)
        return 1

    try:
        registered = registered_subcommands()
    except Exception as exc:  # import/build_parser failure
        print(f"check_readme: could not load ohbs_image CLI: {exc}",
              file=sys.stderr)
        return 2

    import ohbs_image
    profiles = set(ohbs_image.PROFILES.keys())

    readme_text = readme_path.read_text(encoding="utf-8")
    errors = check_readme(readme_text, registered, profiles, ohbs_image.VERSION)

    errors += check_profile_count_in_packaging()

    if args.check_tests:
        errors += check_test_consistency(registered, profiles)

    if args.check_translations:
        # Contract differs by document type: README.md must cover every
        # subcommand, but the translated READMEs (zh-CN / ja / th) are
        # condensed quick-start documents by design — forcing full coverage
        # would make them unusably long. For translations we enforce the
        # *no stale references* contract instead: every `ohbs-image <cmd>`
        # they mention must still exist in the CLI, so a renamed or removed
        # command cannot leave a dead reference behind in any language.
        for name in ("README.zh-CN.md", "README.ja.md", "README.th.md"):
            tpath = REPO_ROOT / name
            if not tpath.exists():
                errors.append(f"{name}: translation README not found")
                continue
            ttext = tpath.read_text(encoding="utf-8")
            stale = translation_stale_commands(ttext, registered)
            if stale:
                errors.append(f"{name} references unknown subcommand(s): "
                              + ", ".join(stale))

    if errors:
        print("check_readme: documentation is out of date with the code:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        print("  Update README.md / tests to keep docs current.",
              file=sys.stderr)
        return 1

    print(f"check_readme: README documents all {len(registered)} subcommands "
          f"and {len(profiles)} profiles OK")
    return 0


def check_test_consistency(registered: set[str],
                           profiles: set[str]) -> list[str]:
    """Ensure tests/test_check_readme.py's hardcoded expected lists match the
    live CLI, so a command/profile added to the parser but not to the tests
    (or vice-versa) is caught instead of silently drifting."""
    errors: list[str] = []

    test_file = REPO_ROOT / "tests" / "test_check_readme.py"
    if not test_file.exists():
        errors.append(f"test file not found: {test_file}")
        return errors

    text = test_file.read_text(encoding="utf-8")

    # ALL_CMDS: a Python set literal in the test, e.g.
    #   ALL_CMDS = {"audit", "build", ..., "verify-image"}
    m = re.search(r"ALL_CMDS\s*=\s*\{(.*?)\}", text, re.DOTALL)
    test_cmds: set[str] = set()
    if m:
        test_cmds = set(re.findall(r"['\"]([A-Za-z0-9_-]+)['\"]", m.group(1)))
    missing_cmds = sorted(registered - test_cmds)
    if missing_cmds:
        errors.append("tests/test_check_readme.py ALL_CMDS is missing command(s): "
                      + ", ".join(missing_cmds))
    extra_cmds = sorted(test_cmds - registered)
    if extra_cmds:
        errors.append("tests/test_check_readme.py ALL_CMDS lists unknown command(s): "
                      + ", ".join(extra_cmds))

    # The test references the script's own _PROFILE_NAMES via
    # `check_readme._PROFILE_NAMES`, so it can't drift from this module; the
    # canonical list here is _PROFILE_NAMES. Verify the test references it.
    if "check_readme._PROFILE_NAMES" not in text:
        errors.append("tests/test_check_readme.py no longer references "
                      "check_readme._PROFILE_NAMES for profile checks")

    return errors


if __name__ == "__main__":
    sys.exit(main())
