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
 * [if] a ramp command names an unsupported control (e.g. 'master')
 *   [then ⛔] it must still be rejected - filter is an addition, not a
 *   blanket allow.
 * [if] a ramp names 'crossfader' or 'tempo' (AGENT-24) [then ⛔] it used to
 *   be rejected, so a plan could not sweep the crossfader or nudge tempo.
 */
import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// The engine reads the page URL for ?extroute=; these tests run as a page with no query.
globalThis.location ??= { search: '' };

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

test('agent ramp drives the CROSSFADER from its current position (AGENT-24)', async () => {
	globalThis.window = { setTimeout: (fn, ms) => setTimeout(fn, ms) };
	const uninstall = entry.installPerformanceBrowserIpc();
	try {
		const result = await entry.executeAgentOrder({
			kind: 'ramp',
			payload: { command: { type: 'crossfader', value: 0 }, to: 0.9, over: { unit: 'ms', n: 20, clock: 1 } }
		});
		assert.equal(result.steps[0].status, 'succeeded');
		assert.equal(entry.queryPerformanceState().mixer.crossfader, 0.9);
	} finally {
		uninstall();
	}
});

test('agent tempo ramp travels in ratio units and refuses a 0..1 reading of them (AGENT-24)', async () => {
	globalThis.window = { setTimeout: (fn, ms) => setTimeout(fn, ms) };
	const uninstall = entry.installPerformanceBrowserIpc();
	try {
		await assert.rejects(
			() =>
				entry.executeAgentOrder({
					kind: 'ramp',
					payload: { command: { type: 'tempo', deck: 1, ratio: 1 }, to: 0, over: { unit: 'ms', n: 20 } }
				}),
			/tempo ramp to must be a ratio in \(0, 4\]/
		);
		await assert.rejects(
			() =>
				entry.executeAgentOrder({
					kind: 'ramp',
					payload: { command: { type: 'fader', deck: 1, value: 1 }, to: 1.04, over: { unit: 'ms', n: 20 } }
				}),
			/ramp to must be a finite 0..1 control value/
		);
	} finally {
		uninstall();
	}
});

test('agent ramp still rejects a control that is not rampable', async () => {
	globalThis.window = { setTimeout: (fn, ms) => setTimeout(fn, ms) };
	const uninstall = entry.installPerformanceBrowserIpc();
	try {
		await assert.rejects(
			() =>
				entry.executeAgentOrder({
					kind: 'ramp',
					payload: {
						command: { type: 'master', deck: 1 },
						to: 1,
						over: { unit: 'ms', n: 20 }
					}
				}),
			/ramp command must be eq, fader, trim, filter, stem_gain, stem_fx_send, crossfader, or tempo/
		);
	} finally {
		uninstall();
	}
});

test('agent tempo ramp refuses a target outside the deck pitch range before any step moves the deck (AGENT-24)', async () => {
	globalThis.window = { setTimeout: (fn, ms) => setTimeout(fn, ms) };
	const uninstall = entry.installPerformanceBrowserIpc();
	try {
		const pitchBefore = entry.queryPerformanceState().decks[1].pitch;
		await assert.rejects(
			() =>
				entry.executeAgentOrder({
					kind: 'ramp',
					payload: { command: { type: 'tempo', deck: 1, ratio: 1 }, to: 1.5, over: { unit: 'ms', n: 20 } }
				}),
			/tempo ramp to must be inside deck 1's pitch range/
		);
		assert.equal(entry.queryPerformanceState().decks[1].pitch, pitchBefore, 'no step ran before the refusal');
	} finally {
		uninstall();
	}
});
