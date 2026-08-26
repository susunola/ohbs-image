# Ecosystem release process

Terraform Provider releases use tags named `terraform-provider-vX.Y.Z`.
The release workflow runs Go tests and vet, builds six OS/architecture archives,
creates SHA256 checksums, signs the checksum manifest with the Terraform
Registry GPG key and attaches GitHub provenance attestations.

Before the first Registry release, register namespace `susunola/ohbsimage` and
add the public GPG key. Store only the armored private key in the repository
secret `TERRAFORM_REGISTRY_GPG_PRIVATE_KEY`.

Use workflow dispatch for an unsigned snapshot. A snapshot never publishes a
GitHub Release and is suitable for compatibility testing. Creating a real tag
is a release decision and must be explicitly approved.

Third-party extension authors start from `integrations/extension-template` and
must pass `ohbs-image extension verify`. Supported ranges are machine-readable
in `integrations/compatibility-matrix.json`.
