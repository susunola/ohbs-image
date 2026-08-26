# Provider plugin API

ohbs-image exposes a versioned provider protocol so cloud integrations can be
installed independently of the core package. The current contract is `1.0`.
Major versions are compatibility boundaries; minor versions only add optional
behaviour.

Providers declare a name, API version, capabilities, a signed API request
method and a read-only discovery method. Credentials are passed explicitly as
`ProviderCredentials`; implementations must not persist or log them.

```python
from ohbs_image._providers import (
    PROVIDER_API_VERSION, ProviderCapabilities, ProviderCredentials,
)

class ExampleProvider:
    name = "example"
    api_version = PROVIDER_API_VERSION
    capabilities = ProviderCapabilities(compute=True, images=True)

    def request(self, service, action, version, region, params, credentials,
                *, max_retries=3):
        ...

    def discover(self, resource, region, **filters):
        return []
```

Register the class in the provider package:

```toml
[project.entry-points."ohbs_image.providers"]
example = "example_provider:ExampleProvider"
```

Run `ohbs-image provider list` and `ohbs-image provider verify example` before
using it. Duplicate names, incompatible API majors and incomplete providers are
rejected at load time. Tencent Cloud is the built-in reference implementation.
