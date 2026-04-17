#!/usr/bin/env bash
# scripts/check-bundle-size.sh -- enforce library page bundle budget.
# Runs after `pnpm build`; fails if chunks under _app/immutable/ exceed
# 250 KB total gzipped.
set -euo pipefail

BUILD_DIR="build/_app/immutable/chunks"
if [[ ! -d "${BUILD_DIR}" ]]; then
    echo "error: ${BUILD_DIR} not found; run 'pnpm build' first." >&2
    exit 1
fi

TOTAL=0
for f in "${BUILD_DIR}"/*.js; do
    [[ -f "$f" ]] || continue
    SIZE=$(gzip -c "$f" | wc -c | tr -d ' ')
    TOTAL=$((TOTAL + SIZE))
done

LIMIT=256000  # 250 KB
echo "total gzipped: ${TOTAL} bytes (limit: ${LIMIT})"
if [[ ${TOTAL} -gt ${LIMIT} ]]; then
    echo "error: library page bundle exceeds 250 KB gzip budget" >&2
    exit 1
fi
