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

function browser(verdict, latencyMs = 0) {
	return {
		state: 'running',
		output_latency_ms: latencyMs,
		base_latency_ms: 5.8,
		sink_id: '',
		output_context_time_s: 1,
		verdict
	};
}

function snap(combined, browserVerdict = 'ok', latencyMs = 192) {
	return {
		browser: browser(browserVerdict, latencyMs),
		device: {
			device_delivering: combined === 'ok',
			verdict: combined === 'ok' ? 'ok' : combined === 'not_delivering' ? 'not_delivering' : 'unknown',
			reason: combined === 'not_delivering' ? 'device stuck' : combined === 'unknown' ? 'no probe' : null,
			default_device_name: 'Speakers',
			default_device_uid: 'uid',
			io_cycles_advanced: combined === 'ok',
			hal_overload_recent: false,
			probe_available: combined !== 'unknown',
			checked_at: '2026-09-20T00:00:00.000Z'
		},
		combined_verdict: combined
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
		const d = mod.describeAudioOutputHealth(snap('idle', 'idle'));
		assert.equal(d.cssClass, 'idle');
		assert.ok(!/broken/i.test(d.title));
	});

	it('idle while a deck plays muted => says a deck is playing, never "nothing is playing"', () => {
		const d = mod.describeAudioOutputHealth(snap('idle', 'ok'));
		assert.equal(d.cssClass, 'idle');
		assert.match(d.title, /A deck is playing, but the master output is muted or silent/);
		assert.doesNotMatch(d.title, /Nothing is playing/);
	});

	it('idle with no deck playing => says nothing is playing', () => {
		const d = mod.describeAudioOutputHealth(snap('idle', 'idle'));
		assert.match(d.title, /Nothing is playing right now/);
		assert.doesNotMatch(d.title, /A deck is playing/);
	});

	it('ok => accent colour, title states the measured latency', () => {
		const d = mod.describeAudioOutputHealth(snap('ok', 'ok', 192));
		assert.equal(d.cssClass, 'ok');
		assert.ok(d.title.includes('192'), 'title should quote the measured latency');
	});

	it('not_delivering => red, broken explainer and toast', () => {
		const d = mod.describeAudioOutputHealth(snap('not_delivering', 'ok', 192));
		assert.equal(d.cssClass, 'dead');
		assert.ok(/broken/i.test(d.title));
		assert.ok(d.toast);
	});

	it('unknown => dim bar, reason in title', () => {
		const d = mod.describeAudioOutputHealth(snap('unknown', 'ok', 192));
		assert.equal(d.cssClass, 'unknown');
		assert.ok(/unknown/i.test(d.title));
		assert.ok(/no probe/i.test(d.title));
	});
});
