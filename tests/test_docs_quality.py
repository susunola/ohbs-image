from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from ohbs_image import build_parser
from scripts.check_docs import check_documents

ROOT = Path(__file__).resolve().parent.parent


def test_all_markdown_links_and_commands_are_current() -> None:
    commands = set(build_parser()._subparsers._group_actions[0].choices)
    assert check_documents(ROOT, commands) == []


def test_document_checker_reports_broken_link_and_stale_command(tmp_path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "README.md").write_text(
        "[missing](docs/missing.md)\n`ohbs-image retired-command`\n", encoding="utf-8",
    )
    errors = check_documents(tmp_path, {"doctor"})
    assert any("broken local link" in error for error in errors)
    assert any("unknown documented command" in error for error in errors)


def test_critical_documented_commands_execute_offline() -> None:
    commands = (
        ("--help",),
        ("doctor", "--help"),
        ("config", "schema"),
        ("provider", "verify", "aws-contract-poc", "--output", "json"),
        ("extension", "list"),
    )
    for args in commands:
        result = subprocess.run(
            [sys.executable, "-m", "ohbs_image", *args], cwd=ROOT,
            capture_output=True, text=True, timeout=20,
        )
        assert result.returncode == 0, (args, result.stdout, result.stderr)
