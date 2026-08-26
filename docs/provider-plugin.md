# Provider plugin API

ohbs-image exposes a versioned provider protocol so cloud integrations can be
installed independently of the core package. The current contract is `1.0`.
Major versions are compatibility boundaries; minor versions only add optional
behaviour.

Providers declare a name, API version, capabilities, a signed API request
method, and a read-only discovery method. They may additionally declare maturity
and an offline contract self-test. Credentials are passed explicitly as
`ProviderCredentials`; implementations must not persist or log them.

```python
from ohbs_image._providers import (
    PROVIDER_API_VERSION, ProviderCapabilities, ProviderCredentials,
)

class ExampleProvider:
    name = "example"
    api_version = PROVIDER_API_VERSION
    maturity = "production"
    capabilities = ProviderCapabilities(compute=True, images=True)

    def request(self, service, action, version, region, params, credentials,
                *, max_retries=3):
        ...

    def discover(self, resource, region, **filters):
        return []

    def contract_test(self):
        return {"checks": [{"name": "mapping", "passed": True}]}
```

Register the class in the provider package:

```toml
[project.entry-points."ohbs_image.providers"]
example = "example_provider:ExampleProvider"
```

Run `ohbs-image provider list` and `ohbs-image provider verify example` before
using it. Duplicate names, incompatible API majors and incomplete providers are
rejected at load time. Contract checks must be deterministic, offline, and free
of credentials and billable operations.

`maturity` and `contract_test()` are optional additions in API 1.x, so existing
plugins remain compatible. Providers that omit them are reported as
`external-unverified` and are not presented as production-ready.

Tencent Cloud is the production reference implementation. The built-in
`aws-contract-poc` exercises launch, wait, image creation, image copy, cleanup,
and fixture discovery through the same provider boundary. It deliberately does
not sign or send AWS requests and is reported as `production_ready: false`.
This proves API decoupling at zero cloud cost; it does not claim production AWS
support, IAM compatibility, quota behaviour, eventual consistency, or boot success.
