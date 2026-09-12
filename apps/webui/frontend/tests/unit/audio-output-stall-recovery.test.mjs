/**
 * Output-stall recovery orchestration (issue #2155).
 *
 * [if] recoverOutput runs [then] rebind.request then recreateGraph, in that order, once
 * [if] recreate throws [then] audio-output-recreate-failed at error
 */
import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

describe('installOutputStallRecovery', () => {
	let mod;
	before(async () => {
		mod = await loadTypeScriptModule('src/lib/rb/audio-output-stall-recovery.ts');
	});

	it('runs rebind then recreate, in that order, once per episode', async () => {
		const order = [];
		let nowMs = 0;
		const rebind = {
			request: async (reason) => {
				order.push(`rebind:${reason}`);
			},
			uninstall: () => {}
		};
		const recovery = mod.installOutputStallRecovery(rebind, {
			recreateGraph: async () => {
				order.push('recreate');
			},
			pushToast: () => {},
			recordPerfEvent: () => {},
			now: () => nowMs
		});
		await recovery.recover();
		await recovery.recover();
		assert.deepEqual(order, ['rebind:output position stalled', 'recreate']);
	});

	it('records recreate failure at error severity', async () => {
		const calls = [];
		const recovery = mod.installOutputStallRecovery(
			{ request: async () => {}, uninstall: () => {} },
			{
				recreateGraph: async () => {
					throw new Error('boom');
				},
				pushToast: (message, kind) => calls.push(`toast:${kind}:${message.slice(0, 24)}`),
				recordPerfEvent: (kind, _message, severity) => calls.push(`perf:${kind}:${severity}`),
				now: () => 0
			}
		);
		await recovery.recover();
		assert.equal(calls.filter((c) => c === 'perf:audio-output-recreate-failed:error').length, 1);
		assert.equal(calls.filter((c) => c.startsWith('toast:error:')).length, 1);
	});

	it('respects cooldown so a second episode does not recreate immediately', async () => {
		let nowMs = 0;
		let recreates = 0;
		const recovery = mod.installOutputStallRecovery(
			{ request: async () => {}, uninstall: () => {} },
			{
				recreateGraph: async () => {
					recreates += 1;
				},
				pushToast: () => {},
				recordPerfEvent: () => {},
				now: () => nowMs
			}
		);
		await recovery.recover();
		nowMs = 1_000;
		await recovery.recover();
		assert.equal(recreates, 1);
	});
});
