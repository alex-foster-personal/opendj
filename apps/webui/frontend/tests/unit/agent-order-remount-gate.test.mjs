/**
 * Codex (PR #5822, BLOCKING): a remount while the previous plan runner is still
 * disposing must not poll for orders until that disposal settles. Otherwise the
 * new mount can accept a command before the old runner sends its delayed stop,
 * and that stop can cancel the new mount's launch. Asserted in source, because
 * the order poll's wiring has no mount harness: the poll is chained on the
 * retiring disposal, and the disposal is kept rather than dropped.
 */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';

const source = await readFile(new URL('../../src/lib/rb/agent-orders.ts', import.meta.url), 'utf8');

test('a remounted order poll starts only after the retiring plan runner has settled', () => {
	assert.match(source, /_retiringPlanDisposal = disposal;/);
	assert.match(source, /Promise\.all\(\[priorDisposal, _retiringPlanDisposal\]\)/);
	assert.match(
		source,
		/void settled\.then\(\(\) =>[\s\S]*?pollAgentOrders\(page, republish, \(\) => running, leaseId, controller\.signal\)/
	);
	assert.doesNotMatch(source, /void pollAgentOrders\(page, republish, \(\) => running\);/);
});

/**
 * Codex (PR #5822, BLOCKING, agent-orders.ts:450): a claim that lands after its
 * mount has retired must not run. The retiring loop's `isRunning()` was checked
 * before the long poll's await, so the claim ran under the NEW mount's plan epoch,
 * which bypasses the retiring disposal. The claim is refused and reported failed.
 */
test('a claim that arrives after its mount retired is refused, never executed', () => {
	assert.match(
		source,
		/result = isRunning\(\)\s*\?\s*await executeAgentOrder\(next\)\s*:\s*\{ steps: \[\{ status: 'failed', error: PLAN_CLOSED \}\], mirror_delta: \{ changed: \{\} \} \};/
	);
});
