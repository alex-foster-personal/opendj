#!/usr/bin/env node
/**
 * Print the verified local engine root pid for a PERFMODE-15 ratio capture.
 *
 * Reuses PERFMODE-14's `engineRootPids` (renderer-process-sample.ts) as is:
 * loopback origin, the engine's self-reported pid owns the listening port, that
 * pid's own command line runs engine code, and (given an expected pid) it has
 * not changed. Exits non-zero with the reason on stderr on any refusal.
 *
 * usage: node engine_root_pid.mjs <engine-origin> <expected-pid>
 */

import { engineRootPids } from "../../apps/webui/frontend/tests/e2e/support/renderer-process-sample.ts";

const [engine, expected] = process.argv.slice(2);
const expectedPid = Number(expected);
if (!engine || !Number.isInteger(expectedPid) || expectedPid <= 0) {
  console.error("usage: node engine_root_pid.mjs <engine-origin> <expected-pid>");
  process.exit(2);
}

try {
  const roots = await engineRootPids(engine, expectedPid);
  process.stdout.write(`${JSON.stringify({ engine_root_pids: roots })}\n`);
} catch (error) {
  console.error(error instanceof Error ? error.message : String(error));
  process.exit(1);
}
