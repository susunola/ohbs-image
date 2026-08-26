# Native Terraform provider

The provider exposes the read-only `ohbsimage_channel` data source over the
authenticated control-plane API. Resolution fails closed when a channel is
missing, unauthorized, corrupt, or points at an inactive artifact.

```hcl
terraform {
  required_providers {
    ohbsimage = { source = "susunola/ohbsimage" }
  }
}

provider "ohbsimage" {
  endpoint = "https://images.example.com"
  # token defaults to the sensitive OHBS_IMAGE_TOKEN environment variable
}

data "ohbsimage_channel" "stable" {
  bucket  = "rhel10"
  channel = "stable"
}

resource "tencentcloud_instance" "app" {
  image_id = data.ohbsimage_channel.stable.image_id
}
```

Build locally with `go build ./...`. Release binaries should be signed and
published with Terraform Registry checksums.

Snapshot artifacts are available through the `Terraform provider release`
workflow dispatch. Production releases use `terraform-provider-vX.Y.Z` tags
and generate signed checksums plus GitHub provenance. See
[`../../docs/ecosystem-release.md`](../../docs/ecosystem-release.md).
