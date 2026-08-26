# Extension SDK and certification

The extension API separates scanners, signers, notifiers, vulnerability feeds
and distributors from the controller. API `1.x` is backwards compatible;
major versions are explicit migration boundaries.

Every extension declares `ExtensionMetadata`, implements its kind-specific
method and provides a deterministic, network-free `self_test()`. Loading fails
closed on an incompatible major, duplicate name, missing method or failed test.

Entry-point groups are `ohbs_image.scanners`, `ohbs_image.signers`,
`ohbs_image.notifiers`, `ohbs_image.feeds` and `ohbs_image.distributors`.

```python
from ohbs_image._extensions import ExtensionMetadata, ExtensionResult

class ExampleScanner:
    metadata = ExtensionMetadata("example", "1.0", "scanner", ("sbom",))
    def scan(self, artifact):
        return ExtensionResult("ok", {"critical_cves": 0})
    def self_test(self):
        result = self.scan({"artifact_id": "fixture"})
        return {"passed": result.status == "ok", "checks": ["fixture-scan"]}
```

Register `example = "package:ExampleScanner"` under the appropriate project
entry-point group. Run `ohbs-image extension verify scanner example` in CI.
The certification command never calls production cloud APIs.
