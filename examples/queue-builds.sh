#!/bin/bash
# queue-builds.sh — example fleet build runner (sequential or parallel).
#
# Copy to your build host next to a checkout of ohbs-image, fill in the
# OS matrix below, and run:  MAX_PARALLEL=3 bash queue-builds.sh
#
# Lessons baked in:
#  - the build host must run CURRENT code: this script pulls + reinstalls
#    before every build (a plain pip install goes silently stale after
#    git pull — verify any time with scripts/check_install.py);
#  - each build logs to run-<os>.log and appends one status line to
#    queue-status.txt, so a watchdog can follow progress;
#  - MAX_PARALLEL>1 runs builds concurrently (watch your cloud API rate
#    limits and instance quotas).
set -u
REPO=${REPO:-/opt/ohbs-image}
STATUS=${STATUS:-/opt/queue-status.txt}
MAX_PARALLEL=${MAX_PARALLEL:-1}
REGION="${REGION:-ap-guangzhou}"
ZONE="${ZONE:-ap-guangzhou-7}"
VPC="${VPC:-vpc-xxxxxxxx}"
SUBNET="${SUBNET:-subnet-xxxxxxxx}"
SG="${SG:-sg-xxxxxxxx}"

# os profile source_image os_tag benchmark instance_type ssh_port
MATRIX="
ubuntu2404 ubuntu2404 img-xxxxxxxx ubuntu-24.04 CIS-v1.0.0 S5.MEDIUM2 
ubuntu2204 ubuntu2204 img-xxxxxxxx ubuntu-22.04 CIS-v1.0.0 S5.MEDIUM2 
rhel9      rhel9      img-xxxxxxxx rhel-9       CIS-v1.0.0 S5.MEDIUM2 
win2022    win2022    img-xxxxxxxx windows-2022 CIS-v5.1.0 S5.MEDIUM4 
"
LEVEL="${LEVEL:-1}"

build_one() {
  local os="$1" profile="$2" src="$3" ostag="$4" bench="$5" itype="$6" sport="$7"
  cat > "$REPO/ohbs-image-$os.toml" <<TOML
[build]
profile             = "$profile"
region              = "$REGION"
zone                = "$ZONE"
instance_type       = "$itype"
source_image_id     = "$src"
vpc_id              = "$VPC"
subnet_id           = "$SUBNET"
security_group_id   = "$SG"
associate_public_ip = true
[image]
name_prefix  = "${os}-cis-l${LEVEL}"
copy_regions = []
[ohbs]
level = $LEVEL
[cloud]
secret_id_env  = "TENCENTCLOUD_SECRET_ID"
secret_key_env = "TENCENTCLOUD_SECRET_KEY"
[meta]
os_tag    = "$ostag"
benchmark = "$bench"
TOML
  [ -n "$sport" ] && echo "ssh_port = $sport" >> "$REPO/ohbs-image-$os.toml"
  echo "=== $os START $(date -u +%FT%TZ)" >> "$STATUS"
  (
    cd "$REPO" && git pull -q && \
    pip install --no-cache-dir --force-reinstall --root-user-action=ignore -q .
    set -a; source /opt/env; set +a
    # per-OS config + workdir so concurrent builds never share state
    ohbs-image build --config "$REPO/ohbs-image-$os.toml" \
      --workdir "$REPO/workdir-$os" --yes --debug \
      --log-file "/opt/run-$os.log.new" >> "/opt/run-$os.log.new" 2>&1
    rc=$?
    set +a
    mv -f "/opt/run-$os.log.new" "/opt/run-$os.log"
    echo "=== $os DONE rc=$rc $(date -u +%FT%TZ)" >> "$STATUS"
  ) &
  [ "$MAX_PARALLEL" -le 1 ] && wait
}

main() {
  : > "$STATUS"
  # Fail fast on a stale install instead of burning a whole build round.
  python3 "$REPO/scripts/check_install.py" || exit 1
  while read -r os profile src ostag bench itype sport; do
    [ -n "$os" ] || continue
    build_one "$os" "$profile" "$src" "$ostag" "$bench" "$itype" "$sport"
    while [ "$(jobs -r | wc -l)" -ge "$MAX_PARALLEL" ]; do sleep 5; done
  done <<< "$MATRIX"
  wait
  echo "=== QUEUE COMPLETE $(date -u +%FT%TZ)" >> "$STATUS"
}
main "$@"
