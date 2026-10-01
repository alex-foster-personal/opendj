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

function browser(verdict, latencyMs = 12.5) {
	return {
		state: 'running',
		output_latency_ms: latencyMs,
		base_latency_ms: 5.8,
		sink_id: '',
		output_context_time_s: 1,
		verdict
	};
}

function snap(combined, browserVerdict = combined === 'ok' ? 'ok' : combined, latencyMs = 12.5) {
	const device =
		combined === 'unknown'
			? {
					device_delivering: null,
					verdict: 'unknown',
					reason: 'no probe',
					default_device_name: null,
					default_device_uid: null,
					io_cycles_advanced: null,
					hal_overload_recent: null,
					probe_available: false,
					checked_at: '2026-09-20T00:00:00.000Z'
				}
			: {
					device_delivering: combined === 'ok',
					verdict: combined === 'ok' ? 'ok' : 'not_delivering',
					reason: combined === 'not_delivering' ? 'stuck' : null,
					default_device_name: 'Speakers',
					default_device_uid: 'uid',
					io_cycles_advanced: combined === 'ok',
					hal_overload_recent: false,
					probe_available: true,
					checked_at: '2026-09-20T00:00:00.000Z'
				};
	return {
		browser: browser(browserVerdict, latencyMs),
		device,
		combined_verdict: combined
	};
}

/**
 * A ring row as `recordPerfEvent` writes one.
 *
 * `severity` is part of the row, not decoration: it is what separates
 * `audio-output-dead` from `audio-output-alive` now that both are audio-domain
 * kinds. `undefined` is deliberately reachable, because rows written before
 * severity was persisted are still in real localStorage.
 */
function evt(kind, ageMs, message = 'something', severity = 'error') {
	const row = { t: new Date(NOW - ageMs).toISOString(), kind, deck: null, message };
	return severity === undefined ? row : { ...row, severity };
}

describe('buildAudioHealthMirror', () => {
	let build;
	before(async () => {
		const mod = await loadTypeScriptModule('src/lib/rb/audio-health-mirror.ts');
		build = mod.buildAudioHealthMirror;
	});

	it('mirrors the device probe block verbatim (issue #923)', () => {
		const merged = snap('not_delivering', 'ok', 192);
		const out = build({
			snapshot: merged,
			rms: 0.3,
			rmsAgeMs: 50,
			silenceVerdict: 'ok',
			events: [],
			nowMs: NOW
		});
		assert.deepEqual(out.device, merged.device);
	});

	it('keeps a fault that fired long past the toast lifetime', () => {
		const out = build({
			snapshot: snap('not_delivering', 'dead', 0),
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
			snapshot: snap('not_delivering', 'dead', 0),
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
		assert.equal(out.output.combined_verdict, 'ok');
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
			events: [evt('deck-load-timing', 1000), evt('audio-output-dead', 2000)],
			nowMs: NOW
		});
		assert.deepEqual(out.recent_faults.map((r) => r.kind), ['audio-output-dead']);
	});

	it('does not report a healthy audio row as a fault', () => {
		// Codex, Thu 10 Sep 2026. Selecting on the `audio-` PREFIX made a
		// perfectly healthy startup publish a nonempty `recent_faults`: the
		// device-floor row, the liveness row and the recovery row are all
		// audio-domain rows that report that things are FINE. An agent reading
		// this block to answer "is anything wrong" was told yes, always.
		const out = build({
			snapshot: snap('ok'), rms: 0.3, rmsAgeMs: 50, silenceVerdict: 'ok',
			events: [
				evt('audio-context', 3000, 'sample_rate_hz=48000', 'info'),
				evt('audio-output-alive', 2000, 'output is back', 'info'),
				evt('audio-output-rebound', 1500, 're-bound after devicechange', 'info'),
				evt('presentation-clock-recovered', 1000, 'clock caught up', 'info')
			],
			nowMs: NOW
		});
		assert.deepEqual(out.recent_faults, [], 'healthy audio rows are not faults');
	});

	it('still reports a real audio fault, which is the overshoot control', () => {
		// The opposite mutation: a rule tightened until nothing qualifies
		// satisfies "no false faults" perfectly and publishes an empty timeline
		// through an outage.
		const out = build({
			snapshot: snap('not_delivering', 'dead', 0), rms: 0, rmsAgeMs: 50, silenceVerdict: 'silent-while-playing',
			events: [
				evt('audio-output-alive', 4000, 'output is back', 'info'),
				evt('audio-output-dead', 3000, 'rendering into a dead output', 'error'),
				evt('audio-output-rebind-deferred', 2000, 'deferred while playing', 'warn'),
				evt('silent-while-playing', 1000, 'nothing leaving the master bus', 'error')
			],
			nowMs: NOW
		});
		assert.deepEqual(
			out.recent_faults.map((r) => r.kind),
			['silent-while-playing', 'audio-output-rebind-deferred', 'audio-output-dead'],
			'every non-info audio row is a fault, newest first'
		);
		assert.deepEqual(out.recent_faults.map((r) => r.severity), ['error', 'warn', 'error']);
	});

	it('carries a row written before severity existed as unknown, not as healthy', () => {
		// A row already in localStorage from an older build. Dropping it would
		// lose a real outage; calling it healthy would be the absent-measurement
		// -reads-as-good defect this whole module exists to remove. It is
		// labelled instead, so a reader can tell it from a diagnosed fault.
		const out = build({
			snapshot: snap('ok'), rms: 0.3, rmsAgeMs: 50, silenceVerdict: 'ok',
			events: [evt('audio-output-dead', 1000, 'legacy row', null)],
			nowMs: NOW
		});
		assert.equal(out.recent_faults.length, 1);
		assert.equal(out.recent_faults[0].severity, 'unknown');
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

/**
 * THE DURABILITY CLAIM, TESTED AGAINST THE RING THAT HAS TO HONOUR IT.
 *
 * `recent_faults` reads the perf ring precisely so a fault outlives its toast.
 * That is only true if the ring still HOLDS the row when the mirror asks. Until
 * Thu 10 Sep 2026 audio faults shared the 8-row `other` bucket with toast rows,
 * timing rows and the successful states `audio-output-alive` and
 * `audio-output-rebound` -- so eight ordinary rows after an outage, the outage
 * row was gone and `recent_faults` published `[]`, which is the same empty
 * reading the live-toast-store version produced. Fixing where the mirror READS
 * without fixing what the ring KEEPS would have moved the defect, not closed it.
 *
 * [if] an audio fault is evicted by unrelated ring traffic [then] fail, [else stop].
 */
describe('audio faults survive unrelated ring traffic', () => {
	let ring;
	before(async () => {
		ring = await loadTypeScriptModule('src/lib/rb/perf-event-log.ts');
	});

	it('keeps the outage row behind a flood of ordinary events', () => {
		ring.recordPerfEvent('audio-output-dead', 'rendering into a dead output', null, 'error');
		// Comfortably past the 8-row shared remainder these used to share.
		for (let i = 0; i < 40; i++) {
			ring.recordPerfEvent(`toast-info`, `ordinary event ${i}`, null, 'info');
			ring.recordPerfEvent(`beat-sync-skip`, `ordinary event ${i}`, null, 'warn');
		}
		const kinds = ring.readPerfEvents().map((row) => row.kind);
		assert.ok(
			kinds.includes('audio-output-dead'),
			`the fault the mirror promises to still be showing was evicted; ring holds: ${kinds.join(', ')}`
		);
	});

	it('does not give every kind unlimited retention, which would be the overshoot', () => {
		for (let i = 0; i < 40; i++) {
			ring.recordPerfEvent('beat-sync-skip', `filler ${i}`, null, 'warn');
		}
		const rows = ring.readPerfEvents().filter((row) => row.kind === 'beat-sync-skip');
		assert.ok(
			rows.length < 40,
			`ordinary kinds must still be bounded, got ${rows.length} retained`
		);
	});

	it('keeps the outage row behind sixteen HEALTHY audio rows', () => {
		// The eviction Codex found after the first retention fix. The dedicated
		// bucket holds 16, and `audio-output-alive` is emitted on every recovery,
		// so a prefix-keyed bucket let the good news flush the bad news out of
		// the one place the mirror looks.
		ring.recordPerfEvent('audio-output-dead', 'the outage under test', null, 'error');
		for (let i = 0; i < 24; i++) {
			ring.recordPerfEvent('audio-output-alive', `recovery ${i}`, null, 'info');
			ring.recordPerfTiming('audio-context', { sample_rate_hz: 48000 });
		}
		const kinds = ring.readPerfEvents().map((row) => row.kind);
		assert.ok(
			kinds.includes('audio-output-dead'),
			`healthy audio rows evicted the outage; ring holds: ${kinds.join(', ')}`
		);
	});

	it('agrees with the mirror about which rows are audio-health faults', () => {
		// One predicate, imported by the mirror. Two copies could let the ring
		// retain rows the fold ignores, or evict rows it looks for.
		const at = (kind, severity) => ring.audioHealthFaultSeverity({
			t: new Date(NOW).toISOString(), kind, deck: null, message: 'x', severity
		});
		assert.equal(at('audio-output-dead', 'error'), 'error');
		assert.equal(at('audio-output-rebind-deferred', 'warn'), 'warn');
		assert.equal(at('silent-while-playing', 'error'), 'error');
		assert.equal(at('presentation-clock-stalled', 'error'), 'error');
		assert.equal(at('audio-output-alive', 'info'), null, 'a recovery is not a fault');
		assert.equal(at('audio-context', 'info'), null, 'a device floor is not a fault');
		assert.equal(at('xrun', 'error'), null, 'xrun has its own counter and would crowd the bucket');
		assert.equal(at('deck-load', 'error'), null, 'not an audio-health row at all');
		assert.equal(at('audio-output-dead', undefined), 'unknown', 'a legacy row is unknown, not healthy');
	});

	it('persists the severity it recorded, so a reload can still tell the two apart', () => {
		ring.recordPerfEvent('audio-output-dead', 'persisted fault', null, 'error');
		ring.recordPerfEvent('audio-output-alive', 'persisted recovery', null, 'info');
		const rows = ring.readPerfEvents();
		const fault = rows.findLast((row) => row.kind === 'audio-output-dead');
		const alive = rows.findLast((row) => row.kind === 'audio-output-alive');
		assert.equal(fault.severity, 'error');
		assert.equal(alive.severity, 'info');
	});
});
