/**
 * Waveform playhead paint-position interpolation (pin 53ba89ca8ddc).
 *
 * [if] the raw sample is unchanged and playing+trusted [then] the painted
 *   position projects forward at `rate` from the last raw sample's own
 *   arrival time, not the caller's arbitrary "now" - it must be exact
 *   interpolation, not a guess
 * [if] a fresh raw sample arrives [then] the state re-anchors to it
 *   immediately (zero-elapsed paint equals the new raw value exactly), so no
 *   drift or blur ever accumulates across samples
 * [if] not playing, or the clock is not trusted [then] the raw value is
 *   painted with zero adjustment - broken otherwise (a real stall or a
 *   paused deck must never be interpolated, that would hide it)
 * [if] the deck is scrubbing [then] `paintPositionMsFor` returns the scrub
 *   preview verbatim and never touches `interpolatedPositionMs` at all - the
 *   user's own drag is ground truth, never projected
 */
import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

describe('interpolatedPositionMs', () => {
	let mod;
	before(async () => {
		mod = await loadTypeScriptModule('src/lib/components/rb/wave/paint-position.ts');
	});

	it('projects forward at `rate` between raw samples, anchored to the sample\'s own arrival time', () => {
		const state = mod.initPositionInterpolatorState();
		// First observation of a raw sample at t=1000ms: zero elapsed, paints raw exactly.
		assert.equal(mod.interpolatedPositionMs(state, 5000, 1000, true, true, 1), 5000);
		// 10ms later, same raw sample (contextTime has not advanced this frame,
		// exactly the measured stall behaviour): projects forward at rate 1.
		assert.equal(mod.interpolatedPositionMs(state, 5000, 1010, true, true, 1), 5010);
		assert.equal(mod.interpolatedPositionMs(state, 5000, 1016, true, true, 1), 5016);
	});

	it('re-anchors instantly on a fresh raw sample - no overshoot, no lag', () => {
		const state = mod.initPositionInterpolatorState();
		mod.interpolatedPositionMs(state, 5000, 1000, true, true, 1);
		mod.interpolatedPositionMs(state, 5000, 1016, true, true, 1); // painted 5016 (projected)
		// A new raw sample arrives (contextTime advanced) carrying the real
		// ground truth 5017 - a hair different from what was being projected.
		// The very next frame must paint exactly the new raw value, not a
		// blend with the stale projection.
		const painted = mod.interpolatedPositionMs(state, 5017, 1017, true, true, 1);
		assert.equal(painted, 5017, 'a fresh raw sample must be painted exactly, never blended');
		// And projection resumes from THIS sample's arrival time, not the old one.
		assert.equal(mod.interpolatedPositionMs(state, 5017, 1027, true, true, 1), 5027);
	});

	it('honours a non-1 rate (deck.pitch), matching WaveRow\'s own scrollPx rate', () => {
		const state = mod.initPositionInterpolatorState();
		mod.interpolatedPositionMs(state, 5000, 1000, true, true, 1.08);
		const painted = mod.interpolatedPositionMs(state, 5000, 1010, true, true, 1.08);
		assert.ok(Math.abs(painted - (5000 + 10 * 1.08)) < 1e-9);
	});

	it('never interpolates while not playing - paints raw exactly', () => {
		const state = mod.initPositionInterpolatorState();
		mod.interpolatedPositionMs(state, 5000, 1000, true, true, 1);
		const painted = mod.interpolatedPositionMs(state, 5000, 1500, false, true, 1);
		assert.equal(painted, 5000, 'a paused deck must show its exact raw position, not a projection');
	});

	it('never interpolates while the clock is untrusted - a real stall stays visible', () => {
		const state = mod.initPositionInterpolatorState();
		mod.interpolatedPositionMs(state, 5000, 1000, true, true, 1);
		// Deck still "playing" (desired intent) but the presentation clock has
		// stalled - trusted=false. Painting must freeze on the raw value,
		// never smooth over the freeze the way a lossy filter would.
		const painted = mod.interpolatedPositionMs(state, 5000, 5000, true, false, 1);
		assert.equal(painted, 5000, 'if a stalled clock is still interpolated then the freeze is hidden from the operator - broken');
	});

	it('resumes clean interpolation once the clock is trusted again, from the current raw sample', () => {
		const state = mod.initPositionInterpolatorState();
		mod.interpolatedPositionMs(state, 5000, 1000, true, true, 1);
		mod.interpolatedPositionMs(state, 5000, 5000, true, false, 1); // stalled window
		// Recovery: a fresh raw sample plus trust restored.
		mod.interpolatedPositionMs(state, 5100, 5001, true, true, 1);
		const painted = mod.interpolatedPositionMs(state, 5100, 5011, true, true, 1);
		assert.equal(painted, 5110);
	});
});

describe('paintPositionMsFor', () => {
	let mod;
	before(async () => {
		mod = await loadTypeScriptModule('src/lib/components/rb/wave/paint-position.ts');
	});

	it('returns the scrub preview verbatim, bypassing interpolation entirely', () => {
		const state = mod.initPositionInterpolatorState();
		mod.interpolatedPositionMs(state, 5000, 1000, true, true, 1);
		const painted = mod.paintPositionMsFor(state, 7777, 5000, 1010, true, true, 1);
		assert.equal(painted, 7777, 'a scrub in progress must win outright, never blended with the raw/projected position');
	});

	it('falls through to interpolatedPositionMs when not scrubbing', () => {
		const state = mod.initPositionInterpolatorState();
		mod.paintPositionMsFor(state, null, 5000, 1000, true, true, 1);
		const painted = mod.paintPositionMsFor(state, null, 5000, 1010, true, true, 1);
		assert.equal(painted, 5010, 'null scrubPreviewMs must defer to the same projection interpolatedPositionMs would paint');
	});
});

describe('paintPositionMs (WaveRow.svelte\'s own call shape)', () => {
	let mod;
	before(async () => {
		mod = await loadTypeScriptModule('src/lib/components/rb/wave/paint-position.ts');
	});

	it('reads position_ms/playing/pitch off a deck-shaped object and negates clockUntrusted into trusted', () => {
		const state = mod.initPositionInterpolatorState();
		const deck = { position_ms: 5000, playing: true, pitch: 1.08 };
		mod.paintPositionMs(state, null, deck, false, 1000); // clockUntrusted=false -> trusted
		const painted = mod.paintPositionMs(state, null, deck, false, 1010);
		assert.ok(Math.abs(painted - (5000 + 10 * 1.08)) < 1e-9, 'must project at deck.pitch once anchored, same as interpolatedPositionMs directly');
	});

	it('freezes on the raw value while clockUntrusted is true, matching interpolatedPositionMs(trusted=false)', () => {
		const state = mod.initPositionInterpolatorState();
		const deck = { position_ms: 5000, playing: true, pitch: 1 };
		mod.paintPositionMs(state, null, deck, false, 1000);
		const painted = mod.paintPositionMs(state, null, deck, true, 5000); // clockUntrusted=true -> trusted=false
		assert.equal(painted, 5000, 'clockUntrusted=true must negate to trusted=false, never interpolate through a stall');
	});

	it('still lets a scrub preview win outright over a deck-shaped call', () => {
		const state = mod.initPositionInterpolatorState();
		const deck = { position_ms: 5000, playing: true, pitch: 1 };
		const painted = mod.paintPositionMs(state, 9999, deck, false, 1000);
		assert.equal(painted, 9999, 'scrubPreviewMs must still short-circuit through the deck-shaped wrapper');
	});
});
