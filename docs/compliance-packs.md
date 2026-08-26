# Compliance evidence packs

`ohbs-image compliance assess ARTIFACT --profile mlps-2.0` maps collected image
evidence to technical controls associated with GB/T 22239-2019. The
`xinchuang-readiness` profile checks domestic-OS coverage, offline operation,
supply-chain traceability and project-specific manual compatibility evidence.

Both profiles generate JSON and printable HTML. A `gap` means required evidence
is absent; `manual` means a human or project-specific test is required. The
output is an engineering evidence aid, not an MLPS grading result, Xinchuang
catalog listing, accredited assessment or certification.

Mappings are versioned package data under `ohbs_image/compliance/`. Organizations
should review them with their assessor and add project-specific overlays rather
than treating a generic mapping as legal advice.
