# Troubleshooting and support bundles

Start with the least invasive diagnostic path:

```bash
ohbs-image doctor --offline
ohbs-image doctor --only toolchain --offline
ohbs-image config validate
ohbs-image config explain --all
```

`doctor --offline` never performs network or cloud checks. A failed check says
what failed and gives a suggested correction. Exit code `1` means readiness is
blocked; exit code `2` means configuration or support-bundle creation failed.

## Create a shareable bundle

```bash
ohbs-image doctor --offline --support-bundle ./ohbs-support.zip
```

The command intentionally refuses to overwrite an existing path. The archive
is written atomically with filesystem mode `0600` and contains exactly:

- `doctor.json`: redacted check results and stable exit code;
- `system.json`: product, Python, operating-system, release, and architecture
  metadata;
- `manifest.json`: SHA-256 and byte length for each diagnostic member;
- `README.txt`: the bundle's privacy boundary.

Configuration files, environment variables, cloud API responses, state,
application logs, home-directory paths, and credentials are not collected.
Redaction is defence in depth rather than permission to share blindly: inspect
the archive before sending it outside your organisation.

Verify the member hashes before analysis:

```bash
python -c 'import hashlib,json,zipfile; z=zipfile.ZipFile("ohbs-support.zip"); m=json.loads(z.read("manifest.json")); assert all(hashlib.sha256(z.read(x["path"])).hexdigest()==x["sha256"] for x in m["members"]); print("bundle verified")'
```

## Useful failure information

When opening an issue, include the verified bundle and describe:

1. the exact command and expected outcome;
2. whether the operation could create billable resources;
3. the run ID, not copied credentials or configuration contents;
4. whether retry or cleanup was attempted;
5. the earliest version known to work.

Do not attach `.env`, `wbenv`, cloud credential files, RBAC token files, or raw
state directories.
