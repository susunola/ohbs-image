from __future__ import annotations

import json
from pathlib import Path


def test_domain_schemas_are_json_schema_2020_12_with_stable_ids():
    paths = sorted(Path("schemas/v1").glob("*.schema.json"))
    assert {path.stem for path in paths} == {
        "artifact.schema", "channel.schema", "operation.schema", "policy.schema"}
    for path in paths:
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert doc["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert doc["$id"].endswith(f"/v1/{path.name}")
        assert doc["type"] == "object"


def test_openapi_31_references_versioned_domain_schemas():
    content = Path("api/openapi.yaml").read_text(encoding="utf-8")
    assert "openapi: 3.1.0" in content
    assert "operationId: promoteChannel" in content
    assert "Idempotency-Key" in content
    for name in ("artifact", "channel"):
        assert f"../schemas/v1/{name}.schema.json" in content


def test_compatibility_policy_requires_major_version_for_breaking_changes():
    content = Path("schemas/COMPATIBILITY.md").read_text(encoding="utf-8")
    assert "breaking changes require a new major" in content
    assert "consumers must ignore unknown fields" in content
