#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


def build(directory: Path) -> None:
    artifacts = sorted(path for path in directory.iterdir() if path.is_file())
    components = []
    checksum_lines = []
    for artifact in artifacts:
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        checksum_lines.append(f"{digest}  {artifact.name}")
        components.append({
            "type": "file",
            "name": artifact.name,
            "hashes": [{"alg": "SHA-256", "content": digest}],
        })
    (directory / "SHA256SUMS").write_text("\n".join(checksum_lines) + "\n", encoding="utf-8")
    sbom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": "urn:uuid:00000000-0000-4000-8000-000000000000",
        "version": 1,
        "components": components,
    }
    (directory / "sbom.cdx.json").write_text(
        json.dumps(sbom, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    build(Path(sys.argv[1] if len(sys.argv) > 1 else "dist"))
