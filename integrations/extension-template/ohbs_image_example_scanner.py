from ohbs_image._extensions import ExtensionMetadata, ExtensionResult


class ExampleScanner:
    metadata = ExtensionMetadata(
        name="example-scanner", api_version="1.0", kind="scanner",
        capabilities=("fixture",), vendor="example",
    )

    def scan(self, artifact):
        return ExtensionResult("ok", {"artifact_id": artifact["artifact_id"],
                                      "critical_cves": 0})

    def self_test(self):
        result = self.scan({"artifact_id": "certification-fixture"})
        return {"passed": result.status == "ok" and result.facts["critical_cves"] == 0,
                "checks": ["fixture-contract", "result-schema"]}
