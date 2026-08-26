# ohbs-image channel data module

This module resolves a hash-verified channel and optionally enforces an
organization policy before Terraform can use the image ID. It performs no cloud
writes. Install `ohbs-image`, synchronize the registry state, and configure:

```hcl
module "golden_image" {
  source        = "./integrations/terraform/modules/ohbs-image-channel"
  bucket        = "rhel10"
  channel       = "stable"
  environment   = "production"
  policy_bundle = "${path.root}/organization-policy.json"
}

resource "tencentcloud_instance" "app" {
  image_id = module.golden_image.image_id
  # ...
}
```

The external program exits non-zero if the channel is missing, its hashes fail,
the artifact is inactive, or policy denies any unexcepted control.
