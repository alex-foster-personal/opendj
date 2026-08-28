#!/bin/bash
# Refresh the rebuild-agentb-from-fable ledger lease on :8585 (plan s15 K0.2).
set -euo pipefail
ETAG=$(curl -s -D - -o /dev/null http://127.0.0.1:8585/api/v1/progress | grep -i '^etag:' | tr -d '\r' | cut -d' ' -f2)
curl -s -X PATCH http://127.0.0.1:8585/api/v1/progress/nodes/rebuild-agentb-from-fable \
  -H 'Content-Type: application/json' -H "If-Match: $ETAG" \
  -d '{"build":{"state":"active"}}' -o /dev/null -w 'heartbeat HTTP %{http_code}\n'
