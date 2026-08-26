# Domain schema compatibility

Schemas use immutable major-version directories (`v1`, `v2`). Within one major version:

- producers may add optional fields;
- consumers must ignore unknown fields;
- required fields, enum members, meanings and identifier formats cannot be removed or narrowed;
- integrity hashes always cover unknown fields as stored;
- breaking changes require a new major directory and an explicit migration command;
- API responses advertise their domain schema through the object's `schema` field.

JSON Schema is the source of truth for stored documents. OpenAPI references those schemas and is the source of truth for HTTP paths and status codes.
