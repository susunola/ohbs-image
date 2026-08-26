from __future__ import annotations

import pytest

from ohbs_image._extensions import (
    ExtensionCompatibilityError,
    ExtensionMetadata,
    ExtensionResult,
    certification_document,
    verify_extension,
)


class Scanner:
    metadata = ExtensionMetadata("fixture-scanner", "1.0", "scanner", ("sbom",))

    def scan(self, artifact):
        return ExtensionResult("ok", {"artifact_id": artifact["artifact_id"]})

    def self_test(self):
        result = self.scan({"artifact_id": "fixture"})
        return {"passed": result.status == "ok", "checks": ["fixture-scan"]}


def test_extension_certification_contract() -> None:
    document = certification_document(Scanner())
    assert document["compatible"] is True
    assert document["metadata"]["kind"] == "scanner"


def test_wrong_major_and_missing_method_fail_closed() -> None:
    class Broken:
        metadata = ExtensionMetadata("broken", "2.0", "feed")

        def self_test(self):
            return {"passed": True, "checks": ["nothing"]}

    with pytest.raises(ExtensionCompatibilityError, match="required major"):
        verify_extension(Broken())


def test_failed_self_test_is_rejected() -> None:
    scanner = Scanner()
    scanner.self_test = lambda: {"passed": False, "checks": ["fixture-scan"]}  # type: ignore[method-assign]
    with pytest.raises(ExtensionCompatibilityError, match="self-test failed"):
        verify_extension(scanner)
