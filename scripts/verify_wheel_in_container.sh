#!/usr/bin/env bash
# Verify a freshly built wheel installs and runs in a clean container.
# Usage: scripts/verify_wheel_in_container.sh [dist/*.whl] [EXPECT_ROLES]
# Requires: docker (daemon running). EXPECT_ROLES defaults to 13 (one engine
# payload + one rules.json per role); CI passes the live count derived from a
# source checkout so the assertion cannot drift when a profile is added.
set -euo pipefail

WHEEL="${1:-$(ls dist/ohbs_image-*.whl 2>/dev/null | head -1)}"
EXPECT_ROLES="${2:-13}"
if [ -z "$WHEEL" ]; then
    echo "no wheel found in dist/ — build one first: python -m build --wheel" >&2
    exit 2
fi
if ! [[ "$EXPECT_ROLES" =~ ^[0-9]+$ ]]; then
    echo "EXPECT_ROLES must be an integer, got '$EXPECT_ROLES'" >&2
    exit 2
fi
WHEEL_ABS="$(cd "$(dirname "$WHEEL")" && pwd)/$(basename "$WHEEL")"
WHEEL_NAME="$(basename "$WHEEL")"
echo "== verifying $WHEEL_NAME in python:3.11-slim =="

docker run --rm \
    -e "EXPECT_ROLES=$EXPECT_ROLES" \
    -v "$WHEEL_ABS:/tmp/$WHEEL_NAME:ro" \
    python:3.11-slim \
    bash -euo pipefail -c "
pip install --quiet /tmp/$WHEEL_NAME
echo '--- version ---'
ohbs-image --version
echo '--- help (first 12 lines) ---'
ohbs-image --help | head -12
echo '--- role payload completeness ---'
python3 - <<'PYEOF'
import os, pathlib, sys
expect = int(os.environ.get('EXPECT_ROLES', '13'))
site = pathlib.Path(sys.prefix) / 'lib'
roles = sorted(p for p in site.rglob('ohbs_image/roles/*/files/*') if p.is_file())
engines = [p for p in roles if p.name in ('ohbs_engine.py', 'ohbs_engine.ps1')]
rules = [p for p in roles if p.name == 'rules.json']
print(f'roles engine payloads: {len(engines)} (expect {expect})')
print(f'rules.json catalogs: {len(rules)} (expect {expect})')
profiles = sorted({p.parts[p.parts.index('roles')+1] for p in roles})
print(f'profiles: {len(profiles)} -> {profiles}')
assert len(engines) == expect, 'engine payload incomplete'
assert len(rules) == expect, 'rules catalogs incomplete'
print('payload OK')
PYEOF
echo '--- offline smoke: list profiles ---'
ohbs-image list | head -5
echo '--- offline smoke: doctor (no-cloud) ---'
ohbs-image doctor --no-cloud | head -8 || true
echo 'CONTAINER VERIFY OK'
"
