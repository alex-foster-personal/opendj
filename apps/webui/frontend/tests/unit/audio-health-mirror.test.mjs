/**
 * The agent-readable audio-health block of the UI mirror.
 *
 * ORIGIN (Thu 10 Sep 2026). The app went silent, the operator saw several
 * error toasts and an output-health bar on screen, and the mirror published
 * `toasts: []` with no audio health at all. An agent reading the mirror
 * minutes later concluded the app was reporting nothing wrong.
 *
 * Two separate defects produced that:
 *   1. `ui-mirror.ts:68` mapped the LIVE toast store. Toasts auto-dismiss in
 *      about 5 s and the mirror publishes every 1000 ms, so any fault older
 *      than a few seconds was invisible.
 *   2. `audioOutputHealth.snapshot` -- the exact reading behind the
 *      `output-health-bar` a human can see at `TopBar.svelte:605` -- was never
 *      mirrored at all. An AGENT-02 parity defect: the mirror must carry what
 *      a human reads off the screen, not a subset.
 *
 * [if] a fault fired past the toast lifetime [then] it is still in the mirror
 * [if] there is no output snapshot [then] the block says unknown, never ok
 * [if] the meter reading is stale [then] it is marked not fresh, so a frozen
 *   high rms can never be read as live signal
 * [if] everything is healthy [then] the block says so (it must be able to go
 *   green, or it is a check that cannot pass)
 */
import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const NOW = 1_000_000;

function snap(verdict, latencyMs = 12.5) {
	return {
		state: 'running',
		output_latency_ms: latencyMs,
		base_latency_ms: 5.8,
		sink_id: '',
		output_context_time_s: 1,
		verdict
	};
}

function evt(kind, ageMs, message = 'something') {
	return { t: new Date(NOW - ageMs).toISOString(), kind, deck: null, message };
}

describe('buildAudioHealthMirror', () => {
	let build;
	before(async () => {
		const mod = await loadTypeScriptModule('src/lib/rb/audio-health-mirror.ts');
		build = mod.buildAudioHealthMirror;
	});

	it('keeps a fault that fired long past the toast lifetime', () => {
		const out = build({
			snapshot: snap('dead', 0),
			rms: 0,
			rmsAgeMs: 100,
			silenceVerdict: 'silent-while-playing',
			events: [evt('audio-output-dead', 13 * 60 * 1000, 'rendering into a dead output')],
			nowMs: NOW
		});
		assert.equal(out.recent_faults.length, 1, 'a 13-minute-old audio fault must survive');
		assert.equal(out.recent_faults[0].kind, 'audio-output-dead');
		assert.equal(out.recent_faults[0].age_ms, 13 * 60 * 1000);
	});

	it('reports unknown, never ok, when there is no snapshot', () => {
		const out = build({
			snapshot: null, rms: null, rmsAgeMs: null,
			silenceVerdict: 'ok', events: [], nowMs: NOW
		});
		assert.equal(out.output, null);
		assert.equal(out.display.cssClass, 'unknown');
		assert.notEqual(out.display.cssClass, 'ok');
	});

	it('marks a stale meter not fresh even when its value looks loud', () => {
		const out = build({
			snapshot: snap('dead', 0),
			rms: 0.47,
			rmsAgeMs: 30_000,
			silenceVerdict: 'ok', events: [], nowMs: NOW
		});
		assert.equal(out.meter.fresh, false, 'a 30s-old reading is not live signal');
		assert.equal(out.meter.rms, 0.47, 'the value is still reported, just not trusted');
	});

	it('goes green when everything is healthy', () => {
		const out = build({
			snapshot: snap('ok', 12.5),
			rms: 0.3,
			rmsAgeMs: 80,
			silenceVerdict: 'ok', events: [], nowMs: NOW
		});
		assert.equal(out.display.cssClass, 'ok');
		assert.equal(out.meter.fresh, true);
		assert.equal(out.recent_faults.length, 0);
		assert.equal(out.output.verdict, 'ok');
	});

	it('treats an UNKNOWN meter age as not fresh', () => {
		// Found by mutation: `rmsAgeMs === null ? true : ...` passed the whole
		// suite. An absent measurement rendering as a good one is the same defect
		// shape as `master-silence-report.ts:55` reporting rms 1.0 for a null
		// analyser, which is a large part of why this outage went undiagnosed.
		const out = build({
			snapshot: snap('ok'), rms: null, rmsAgeMs: null,
			silenceVerdict: 'ok', events: [], nowMs: NOW
		});
		assert.equal(out.meter.fresh, false, 'no age reading is not evidence of a fresh one');
	});

	it('ignores unrelated perf events so the block stays about audio', () => {
		const out = build({
			snapshot: snap('ok'), rms: 0.3, rmsAgeMs: 50, silenceVerdict: 'ok',
			events: [evt('deck-load-timing', 1000), evt('audio-context', 2000)],
			nowMs: NOW
		});
		assert.deepEqual(out.recent_faults.map((r) => r.kind), ['audio-context']);
	});

	it('refuses a sample time from the future rather than reporting a negative age', () => {
		assert.throws(
			() => build({
				snapshot: snap('ok'), rms: 0.3, rmsAgeMs: 50, silenceVerdict: 'ok',
				events: [evt('audio-output-dead', -5000)], nowMs: NOW
			}),
			/future/i
		);
	});
});
