from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _load(name: str, filename: str):
    path = Path(__file__).resolve().parents[1] / "scripts" / filename
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


indexer = _load("build_evidence_index", "build_evidence_index.py")
contracts = _load("check_core_contracts", "check_core_contracts.py")


def test_evidence_index_preserves_failure_and_hashes_source(tmp_path: Path) -> None:
    source = tmp_path / "acceptance-result.json"
    source.write_text(json.dumps({
        "schemaVersion": 1, "status": "failed", "profile": "rhel10",
        "commit": "abc123", "runUrl": "https://example.test/run/7",
        "finishedAt": "2026-08-27T00:00:00Z", "evidenceArtifact": "acceptance-7",
    }), encoding="utf-8")
    rows = indexer.collect([tmp_path])
    assert len(rows) == 1
    assert rows[0]["kind"] == "cloud-acceptance"
    assert rows[0]["status"] == "failed"
    assert len(rows[0]["sha256"]) == 64
    document = indexer.build_document(rows, "2026-08-27T01:00:00Z")
    assert document["summary"] == {
        "total": 1, "passed": 0, "failed": 1, "incomplete": 0, "available": 0,
    }
    rendered = indexer.render_html(document, "Evidence <index>")
    assert "Evidence &lt;index&gt;" in rendered
    assert "https://example.test/run/7" in rendered


def test_empty_index_is_not_success() -> None:
    document = indexer.build_document([], "2026-08-27T01:00:00Z")
    assert document["summary"]["total"] == 0
    assert "No evidence supplied" in indexer.render_html(document, "Evidence")


def test_core_contract_snapshot_matches_repository() -> None:
    expected = json.loads(contracts.SNAPSHOT.read_text(encoding="utf-8"))
    assert expected == contracts.current_contracts()
