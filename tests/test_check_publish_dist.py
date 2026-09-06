"""Tests for scripts/check_publish_dist.py — the PyPI upload directory guard."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import check_publish_dist  # noqa: E402


def _touch(directory: Path, name: str) -> None:
    (directory / name).write_text("x", encoding="utf-8")


class TestStrayFiles:
    def test_clean_directory(self, tmp_path):
        _touch(tmp_path, "ohbs_image-0.21.0-py3-none-any.whl")
        _touch(tmp_path, "ohbs_image-0.21.0.tar.gz")
        assert check_publish_dist.stray_files(tmp_path) == []

    def test_checksums_are_strays(self, tmp_path):
        """Regression: SHA256SUMS in the upload dir aborted the v0.20.0 publish
        with `InvalidDistribution: Unknown distribution format`."""
        _touch(tmp_path, "ohbs_image-0.21.0-py3-none-any.whl")
        _touch(tmp_path, "SHA256SUMS")
        _touch(tmp_path, "sbom.cdx.json")
        assert check_publish_dist.stray_files(tmp_path) == [
            "SHA256SUMS",
            "sbom.cdx.json",
        ]

    def test_directories_ignored(self, tmp_path):
        (tmp_path / "nested").mkdir()
        _touch(tmp_path, "ohbs_image-0.21.0-py3-none-any.whl")
        assert check_publish_dist.stray_files(tmp_path) == []


class TestMain:
    def test_ok(self, tmp_path, capsys):
        _touch(tmp_path, "ohbs_image-0.21.0-py3-none-any.whl")
        assert check_publish_dist.main([str(tmp_path)]) == 0
        assert "OK" in capsys.readouterr().out

    def test_stray_fails(self, tmp_path, capsys):
        _touch(tmp_path, "ohbs_image-0.21.0-py3-none-any.whl")
        _touch(tmp_path, "SHA256SUMS")
        assert check_publish_dist.main([str(tmp_path)]) == 1
        assert "SHA256SUMS" in capsys.readouterr().err

    def test_empty_directory_fails(self, tmp_path):
        assert check_publish_dist.main([str(tmp_path)]) == 1

    def test_missing_directory_fails(self, tmp_path):
        assert check_publish_dist.main([str(tmp_path / "nope")]) == 1
