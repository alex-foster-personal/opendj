#!/usr/bin/env bash
# scripts/r2-bootstrap.sh -- create R2 buckets + lifecycle rules. Phase 11,
# plan 11-01 step 7.
#
# Requires `aws` CLI (for s3api). Invoked under `doppler run -- ...` so
# the four R2_* env vars are set. Idempotent: ignores BucketAlreadyOwnedByYou.
set -euo pipefail

if [[ ${EUID} -eq 0 ]]; then
    echo "error: do not run with sudo" >&2
    exit 1
fi

: "${R2_ACCOUNT_ID:?R2_ACCOUNT_ID not set; run under doppler run --}"
: "${R2_ACCESS_KEY_ID:?R2_ACCESS_KEY_ID not set}"
: "${R2_SECRET_ACCESS_KEY:?R2_SECRET_ACCESS_KEY not set}"

STATE_BUCKET="${MUSIC_DJ_STATE_BUCKET:-music-dj-state}"
AUDIO_BUCKET="${MUSIC_DJ_AUDIO_BUCKET:-music-dj-audio}"
ENDPOINT="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"

export AWS_ACCESS_KEY_ID="${R2_ACCESS_KEY_ID}"
export AWS_SECRET_ACCESS_KEY="${R2_SECRET_ACCESS_KEY}"
export AWS_DEFAULT_REGION="auto"

create_bucket() {
    local bucket="$1"
    if aws s3api head-bucket --endpoint-url "${ENDPOINT}" --bucket "${bucket}" 2>/dev/null; then
        echo "bucket '${bucket}' already exists"
        return 0
    fi
    aws s3api create-bucket --endpoint-url "${ENDPOINT}" --bucket "${bucket}" \
        >/dev/null
    echo "created bucket '${bucket}'"
}

apply_wal_lifecycle() {
    local bucket="$1"
    local tmp
    tmp="$(mktemp)"
    cat >"${tmp}" <<JSON
{
  "Rules": [
    {
      "ID": "wal-90day-ttl",
      "Status": "Enabled",
      "Filter": {"Prefix": "wal/"},
      "Expiration": {"Days": 90}
    }
  ]
}
JSON
    aws s3api put-bucket-lifecycle-configuration \
        --endpoint-url "${ENDPOINT}" --bucket "${bucket}" \
        --lifecycle-configuration "file://${tmp}" >/dev/null
    rm -f "${tmp}"
    echo "applied 90-day TTL on ${bucket}/wal/"
}

create_bucket "${STATE_BUCKET}"
create_bucket "${AUDIO_BUCKET}"
apply_wal_lifecycle "${STATE_BUCKET}"

cat <<EOF

R2 bootstrap complete.

Buckets:
  state  -> s3://${STATE_BUCKET}/  (Litestream replicates here)
  audio  -> s3://${AUDIO_BUCKET}/  (OPT-IN; Phase 11 does NOT enable by default)

Phase 11 only uses the state bucket. The audio bucket is reserved for
opt-in cold-start via apps/cloud/s3_audio.py. Enable it later by editing
apps/cloud/README.md's 'Cost model' section first.
EOF
