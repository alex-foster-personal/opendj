#!/usr/bin/env -S node --experimental-strip-types
/**
 * Print the verified local engine root pid for a PERFMODE-15 ratio capture.
 *
 * Reuses PERFMODE-14's `engineRootPids` (renderer-process-sample.ts) as is:
 * loopback origin, the engine's self-reported pid owns the listening port, that
 * pid's own command line runs engine code, and (given an expected pid) it has
 * not changed. Exits non-zero with the reason on stderr on any refusal.
 *
 * It imports a `.ts` module, so it needs type stripping: on by default from
 * Node 22.18, but the repo's floor (package.json engines, CI's pinned node) is
 * 22.14, where a plain `node` refuses with ERR_UNKNOWN_FILE_EXTENSION. The flag
 * is the same one the frontend unit-test scripts pass.
 *
 * usage: node --experimental-strip-types engine_root_pid.mjs <engine-origin> <expected-pid>
 */

import { engineRootPids } from "../../apps/webui/frontend/tests/e2e/support/renderer-process-sample.ts";

const [engine, expected] = process.argv.slice(2);
const expectedPid = Number(expected);
if (!engine || !Number.isInteger(expectedPid) || expectedPid <= 0) {
  console.error("usage: node --experimental-strip-types engine_root_pid.mjs <engine-origin> <expected-pid>");
  process.exit(2);
}

try {
  const roots = await engineRootPids(engine, expectedPid);
  process.stdout.write(`${JSON.stringify({ engine_root_pids: roots })}\n`);
} catch (error) {
  console.error(error instanceof Error ? error.message : String(error));
  process.exit(1);
}
