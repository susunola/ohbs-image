#!/usr/bin/env python3
"""Fail when a PyPI upload directory holds anything but distributions.

``pypa/gh-action-pypi-publish`` validates every file in its packages-dir with
twine, so a stray ``SHA256SUMS`` or ``sbom.cdx.json`` aborts the upload with
``InvalidDistribution: Unknown distribution format`` — which is exactly how the
v0.20.0 release reached GitHub but never reached PyPI.

Release manifests belong on the GitHub Release, not in the upload directory.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

DISTRIBUTION_SUFFIXES = (".whl", ".tar.gz")


def is_distribution(name: str) -> bool:
    return name.endswith(DISTRIBUTION_SUFFIXES)


def stray_files(directory: Path) -> list[str]:
    """Non-distribution files that would break a twine upload."""
    return sorted(
        path.name
        for path in directory.iterdir()
        if path.is_file() and not is_distribution(path.name)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "directory",
        nargs="?",
        default="dist",
        help="Upload directory to validate (default: dist)",
    )
    args = parser.parse_args(argv)
    directory = Path(args.directory)
    if not directory.is_dir():
        print(f"upload directory not found: {directory}", file=sys.stderr)
        return 1
    strays = stray_files(directory)
    if strays:
        print(
            f"{directory} holds {len(strays)} non-distribution file(s): "
            f"{', '.join(strays)}",
            file=sys.stderr,
        )
        print(
            "pypa/gh-action-pypi-publish validates every file here — keep release "
            "manifests (SHA256SUMS, SBOM) in a separate directory.",
            file=sys.stderr,
        )
        return 1
    distributions = sorted(
        path.name for path in directory.iterdir() if path.is_file()
    )
    if not distributions:
        print(f"{directory} holds no distributions", file=sys.stderr)
        return 1
    print(f"upload directory OK: {len(distributions)} distribution(s) in {directory}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
