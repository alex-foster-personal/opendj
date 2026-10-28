/**
 * AGENT-30: a position anchor is valid only on the timeline mapping it was observed on.
 *
 * [if] any timeline change happened after the observation [then ⛔] validAnchor answers null.
 * Engine events and how each reaches this check: play, pause, seek, tempo/pitch, loop on/off all acknowledge a new
 * schedule revision (`_scheduleDeck` -> acknowledgePresentedTransportSchedule); a pending one leaves desired ahead of
 * presented; load and unload replace the timeline object; a loop wrap happens inside one revision (so any engaged loop
 * refuses); a master delay change shifts presentation. Keylock (master tempo) changes the pitch shifter, not the time
 * mapping, and its toggle is a schedule revision too.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const { validAnchor } = await loadTypeScriptModule('src/lib/rb/position-anchor.ts');

const timeline = {};
const recorded = { position_ms: 1000, context_sec: 5, pitch: 1, basis: { timeline, revision: 3, master_delay_ms: 0 } };
const live = { timeline, desired_revision: 3, presented_revision: 3, playing: true, loop_active: false, master_delay_ms: 0 };

test('an anchor on the same timeline, revision, transport and delay is valid (control)', () => {
	assert.deepEqual(validAnchor(recorded, live), { position_ms: 1000, context_sec: 5, pitch: 1 });
});

for (const [event, change] of [
	['seek, tempo/pitch, play or loop toggle (a new presented revision)', { desired_revision: 4, presented_revision: 4 }],
	['any of those still pending (desired ahead of presented)', { desired_revision: 4 }],
	['load or unload (a new timeline object)', { timeline: {} }],
	['pause', { playing: false }],
	['an engaged loop (it wraps inside one revision)', { loop_active: true }],
	['a master delay change', { master_delay_ms: 20 }]
]) {
	test(`${event} invalidates the anchor`, () => {
		assert.equal(validAnchor(recorded, { ...live, ...change }), null);
	});
}

test('no observation yet is no anchor', () => {
	assert.equal(validAnchor(null, live), null);
});

test('the engine pairs the presented position with the graph time it was rendered at, room delay removed', async () => {
	// Codex P2/BLOCKING on #7594: the presented position lags the render by master_delay_ms (CUEOUT-14), and the fader
	// and EQ nodes sit upstream of that delay, so the anchor's context time must have the delay taken off.
	const { readFrontendSource } = await import('./engine-source.mjs');
	const source = await readFrontendSource('src/lib/rb/audio-engine.svelte.ts');
	const at = source.indexOf('_positionAnchor[deck] = observation');
	const record = source.slice(at - 200, at + 400);
	assert.match(record, /const delayMs = mixerState\.headphones\.master_delay_ms;/);
	assert.match(record, /context_sec: outputTimestamp\.contextTime - masterDelaySeconds\(delayMs\)/);
});

test('the engine records no anchor for a sample-clock fallback observation, so steps fire on their bars', async () => {
	// Codex BLOCKING #7594: a stalled output timestamp paired with a sample-clock position projected steps into the past.
	const { readFrontendSource } = await import('./engine-source.mjs');
	const source = await readFrontendSource('src/lib/rb/audio-engine.svelte.ts');
	assert.match(source, /_positionAnchor\[deck\] = observation\.clock_source === 'sample' \? null : \{/);
});

test('a graph reset clears every position anchor; one recorded after it is used as before', async () => {
	// Sol P1 #7594: a pre-reset anchor paired a position with the retired context's clock, so a post-reset step was
	// timed from the wrong clock. Control: the recording path, which writes a fresh anchor after the reset, is unchanged.
	const { readFrontendSource } = await import('./engine-source.mjs');
	const source = await readFrontendSource('src/lib/rb/audio-engine.svelte.ts');
	const reset = source.slice(source.indexOf('function _resetGraphState('), source.indexOf('function _resetGraphState(') + 800);
	assert.match(reset, /_ctx = null; _steps\.cancelAll\(\); for \(const deck of DECK_IDS\) _positionAnchor\[deck\] = null;/);
	assert.match(source, /_positionAnchor\[deck\] = observation\.clock_source === 'sample' \? null : \{/);
	// The plan side of both halves: a null anchor fires a step on its bar, and a valid anchor sends it ahead.
	const recorded = { position_ms: 1000, context_sec: 2, pitch: 1, basis: { timeline: {}, revision: 1, master_delay_ms: 0 } };
	const timeline = recorded.basis.timeline;
	const now = { timeline, desired_revision: 1, presented_revision: 1, playing: true, loop_active: false, master_delay_ms: 0 };
	assert.equal(validAnchor(null, now), null, 'after the reset: no anchor, so the step fires on its bar');
	assert.deepEqual(validAnchor(recorded, now), { position_ms: 1000, context_sec: 2, pitch: 1 }, 'control: a fresh anchor is used');
});
