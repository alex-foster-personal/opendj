/**
 * Issue #990 follow-up (PR #1021, discussion_r3966901263): `_ramp` in
 * agent-orders.ts accepted only eq/fader/trim, so an agent could set FILTER
 * with a single write but could not perform the duration/beat-based sweep
 * that a UI drag can. This exercises the real ramp executor end to end
 * (no mocking of dispatch or state) to prove filter is now a genuine ramp
 * target, not just a source-text match on the allow-list.
 *
 * [if] a ramp command names 'filter' [then ⛔] it used to throw
 *   'ramp command must be eq, fader, or trim' before ever dispatching.
 * [if] a ramp command names an unsupported control (e.g. 'crossfader')
 *   [then ⛔] it must still be rejected - filter is an addition, not a
 *   blanket allow.
 */
import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const entry = await loadTypeScriptModule('tests/unit/fixtures/agent-orders-entry.ts');

afterEach(() => {
	delete globalThis.window;
});

test('agent ramp drives FILTER from start to target value', async () => {
	globalThis.window = { setTimeout: (fn, ms) => setTimeout(fn, ms) };
	const uninstall = entry.installPerformanceBrowserIpc();
	try {
		const result = await entry.executeAgentOrder({
			kind: 'ramp',
			payload: {
				command: { type: 'filter', deck: 1, value: 0 },
				to: 1,
				over: { unit: 'ms', n: 20, clock: 1 }
			}
		});

		assert.equal(result.steps.length, 1);
		assert.equal(result.steps[0].status, 'succeeded');
		assert.equal(entry.queryPerformanceState().mixer.channels[1].filter, 1);
	} finally {
		uninstall();
	}
});

test('agent ramp still rejects a control that is not eq/fader/trim/filter', async () => {
	globalThis.window = { setTimeout: (fn, ms) => setTimeout(fn, ms) };
	const uninstall = entry.installPerformanceBrowserIpc();
	try {
		await assert.rejects(
			() =>
				entry.executeAgentOrder({
					kind: 'ramp',
					payload: {
						command: { type: 'crossfader', value: 0 },
						to: 1,
						over: { unit: 'ms', n: 20 }
					}
				}),
			/ramp command must be eq, fader, trim, or filter/
		);
	} finally {
		uninstall();
	}
});
