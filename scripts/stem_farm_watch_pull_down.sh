#!/usr/bin/env bash
# Poll GCE stem farm, pull tars into data/state/stems, then delete the VM.
set -euo pipefail
# Fail loudly when connection settings are missing: no defaults for host, key or project.
: "${MDT_STEM_HOST:?set MDT_STEM_HOST to the stem farm VM address}"
: "${MDT_STEM_SSH_KEY:?set MDT_STEM_SSH_KEY to the ssh private key path for the stem farm VM}"
: "${MDT_VOCAL_GCE_PROJECT:?set MDT_VOCAL_GCE_PROJECT to the GCE project hosting the stem farm}"
HOST="$MDT_STEM_HOST"
KEY="$MDT_STEM_SSH_KEY"
PROJECT="$MDT_VOCAL_GCE_PROJECT"
ZONE="${MDT_VOCAL_GCE_ZONE:-us-central1-a}"
INSTANCE="${MDT_STEM_INSTANCE:-mdt-stems-cpu}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STEMS="$ROOT/data/state/stems"
PULL="$ROOT/.tmp/.tmp_stem_outbox"
mkdir -p "$STEMS" "$PULL"

ssh_cmd() {
  ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 -i "$KEY" "dev@$HOST" "$@"
}

echo "monitoring $INSTANCE ($HOST) ..."
while true; do
  if ! status="$(ssh_cmd 'tail -n 5 /opt/mdt-stems/logs/farm.log; echo ---; ls /opt/mdt-stems/outbox/*.tar 2>/dev/null | wc -l; ps -ef | grep stem_farm_runner | grep -v grep | wc -l' 2>/dev/null)"; then
    echo "ssh failed; retrying in 60s"
    sleep 60
    continue
  fi
  echo "$(date -u +%H:%M:%SZ) $status"
  running="$(echo "$status" | tail -n 1 | tr -d '[:space:]')"
  tars="$(echo "$status" | tail -n 2 | head -n 1 | tr -d '[:space:]')"
  if [[ "$running" == "0" ]]; then
    echo "farm idle; pulling $tars tars"
    break
  fi
  sleep 90
done

rm -rf "$PULL"
mkdir -p "$PULL"
scp -o StrictHostKeyChecking=accept-new -i "$KEY" -r "dev@$HOST:/opt/mdt-stems/outbox/." "$PULL/"
python3 - <<PY
import tarfile
from pathlib import Path
pull = Path("$PULL")
stems = Path("$STEMS")
n = 0
for tar_path in sorted(pull.glob("*.tar")):
    sid = tar_path.stem
    dest = stems / sid
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path) as tar:
        tar.extractall(dest)
    n += 1
    print(f"unpacked {sid}")
print(f"imported {n} bundles -> {stems}")
PY

echo "deleting $INSTANCE ..."
gcloud compute instances delete "$INSTANCE" --project="$PROJECT" --zone="$ZONE" --quiet
echo "done"
