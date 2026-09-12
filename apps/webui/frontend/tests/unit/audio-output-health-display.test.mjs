/**
 * Pure verdict -> {cssClass, title} mapping for the output-to-device bar
 * under master volume (pin 93c82bb36eb7).
 *
 * [if] there is no snapshot yet [then] "unknown", never a false "ok"
 * [if] verdict is idle [then] the bar goes dark, no error language
 * [if] verdict is ok [then] the bar is the accent colour and the title states
 *   the measured latency
 * [if] verdict is dead or dead-escalated [then] the bar is red and the title
 *   is the broken explainer, escalated adds the reload instruction
 */
import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

function snap(verdict, latencyMs = 0) {
	return {
		state: 'running',
		output_latency_ms: latencyMs,
		base_latency_ms: 5.8,
		sink_id: '',
		output_context_time_s: 1,
		verdict
	};
}

describe('describeAudioOutputHealth', () => {
	let mod;
	before(async () => {
		mod = await loadTypeScriptModule('src/lib/rb/audio-output-health-display.ts');
	});

	it('no snapshot => unknown, never a false ok', () => {
		const d = mod.describeAudioOutputHealth(null);
		assert.equal(d.cssClass, 'unknown');
		assert.ok(!/\bOK\b/.test(d.title));
	});

	it('idle => dark, no error language', () => {
		const d = mod.describeAudioOutputHealth(snap('idle'));
		assert.equal(d.cssClass, 'idle');
		assert.ok(!/broken/i.test(d.title));
	});

	it('ok => accent colour, title states the measured latency', () => {
		const d = mod.describeAudioOutputHealth(snap('ok', 192));
		assert.equal(d.cssClass, 'ok');
		assert.ok(d.title.includes('192'), 'title should quote the measured latency');
	});

	it('dead => red, broken explainer, no reload instruction yet', () => {
		const d = mod.describeAudioOutputHealth(snap('dead'));
		assert.equal(d.cssClass, 'dead');
		assert.ok(/broken/i.test(d.title));
		assert.ok(!/reload/i.test(d.title));
	});

	it('dead-escalated => red, adds the reload instruction', () => {
		const d = mod.describeAudioOutputHealth(snap('dead-escalated'));
		assert.equal(d.cssClass, 'dead');
		assert.ok(/reload/i.test(d.title));
	});

	it('stalled => red, names frozen output position and recovery', () => {
		const d = mod.describeAudioOutputHealth(snap('stalled'));
		assert.equal(d.cssClass, 'dead');
		assert.ok(/output position stopped advancing/i.test(d.title));
		assert.ok(/recovery is running/i.test(d.title));
		assert.ok(!/\bOK\b/.test(d.title));
	});
});
