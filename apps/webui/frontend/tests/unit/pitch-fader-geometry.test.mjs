/**
 * The pitch fader's thumb must sit where its value says, at any track height.
 *
 * Pin ebb1def0234c (the maintainer, Wed 2 Sep 2026): "are all pitch sliders currently
 * off-zero by default? eh?"
 *
 * They were not off-zero. They were drawn off-zero, which is worse, because
 * the number and the picture disagreed and only the picture was wrong.
 *
 * PitchFader hardcoded `TRACK_H = 96` with the comment "matches .rb-fader
 * height in theme.css". It does not: `.rb-fader` is `height: 100%` with a
 * `min-height: 64px`, so the real track is whatever the deck layout gives it.
 * The thumb was positioned as `(1 - value) * (96 - 12)`, so at 0% pitch it
 * landed 42px from the top of a track that is only 96px tall by accident -
 * visibly above centre on any taller one.
 *
 * The input path made it worse by being RIGHT: `_valueFromEvent` measured the
 * real element with getBoundingClientRect and then divided by the same stale
 * constant, so dragging the thumb to the visual centre did not give 0% either.
 *
 * Regression lines:
 * - if the thumb offset stops being expressed against the real track height
 *   then 0% is drawn off centre again on every fader that is not 96px
 * - if the pointer mapping and the render mapping stop agreeing then dropping
 *   the thumb somewhere does not produce the value it was dropped on
 * - if the ends stop pinning exactly then the fader cannot reach its own range
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, describe, it, test } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

let geo;
before(async () => {
	geo = await loadTypeScriptModule('src/lib/rb/pitch-fader-geometry.ts');
});

describe('pitch fader geometry', () => {
	it('centres 0% on any track height, not just 96px', () => {
		const { thumbOffsetPx, THUMB_H } = geo;
		for (const trackH of [64, 96, 120, 200]) {
			const centred = (trackH - THUMB_H) / 2;
			assert.equal(
				thumbOffsetPx(0.5, trackH),
				centred,
				`0% is off-centre on a ${trackH}px track`
			);
		}
	});

	it('pins both ends exactly', () => {
		const { thumbOffsetPx, THUMB_H } = geo;
		assert.equal(thumbOffsetPx(1, 150), 0, 'the top end is not reachable');
		assert.equal(thumbOffsetPx(0, 150), 150 - THUMB_H, 'the bottom end is not reachable');
	});

	it('round-trips a pointer position back to the same value', () => {
		const { thumbOffsetPx, valueFromPointer, THUMB_H } = geo;
		// Drop the thumb where a value draws it, and read that value back.
		for (const trackH of [64, 96, 175]) {
			for (const value of [0, 0.25, 0.5, 0.75, 1]) {
				const top = thumbOffsetPx(value, trackH);
				const back = valueFromPointer(top + THUMB_H / 2, trackH);
				assert.ok(
					Math.abs(back - value) < 1e-9,
					`${value} at ${trackH}px round-tripped to ${back}`
				);
			}
		}
	});

	it('clamps a pointer dragged past either end', () => {
		const { valueFromPointer } = geo;
		assert.equal(valueFromPointer(-500, 96), 1);
		assert.equal(valueFromPointer(5000, 96), 0);
	});

	it('degrades safely on a zero-height track rather than dividing by zero', () => {
		const { thumbOffsetPx, valueFromPointer, THUMB_H } = geo;
		assert.equal(Number.isFinite(thumbOffsetPx(0.5, THUMB_H)), true);
		assert.equal(Number.isFinite(valueFromPointer(10, THUMB_H)), true);
	});
});

// ---------------------------------------------------------------------------
// Review thread https://github.com/maintainer/music-dj-tools/pull/945#discussion_r3917231716
// (P2/NON-BLOCKING): this file used to test faderValueFromPitchRatio,
// pitchRatioFromFaderValue and MIN_TEMPO_RATIO
// (src/lib/components/rb/deck/pitch-fader-geometry.ts) before it was
// repointed to the new pixel-geometry module above. That production module is
// unchanged and still the only thing PitchFader.svelte calls to turn a fader
// value into a tempo ratio - repointing the file deleted its only coverage.
// Restored verbatim (the deleted assertions, not a rewrite) rather than moved
// to a new file, since both describe the same control from two angles: pixel
// placement above, value<->ratio mapping here.
let mapping;
before(async () => {
	mapping = await loadTypeScriptModule('src/lib/components/rb/deck/pitch-fader-geometry.ts');
});

test('center fader value is always 0% pitch regardless of the selected range', () => {
	const { pitchRatioFromFaderValue } = mapping;
	for (const pitchRangePct of [8, 16, 100]) {
		assert.equal(pitchRatioFromFaderValue(0.5, pitchRangePct), 1);
	}
});

test('fader travel spans exactly the selected pitch range', () => {
	const { pitchRatioFromFaderValue, MIN_TEMPO_RATIO } = mapping;
	assert.equal(pitchRatioFromFaderValue(1, 8), 1.08);
	assert.equal(pitchRatioFromFaderValue(0, 8), 0.92);
	assert.equal(pitchRatioFromFaderValue(1, 16), 1.16);
	assert.equal(pitchRatioFromFaderValue(0, 16), 0.84);
	assert.equal(pitchRatioFromFaderValue(1, 100), 2);
	assert.equal(pitchRatioFromFaderValue(0, 100), MIN_TEMPO_RATIO);
	assert.ok(pitchRatioFromFaderValue(0, 100) > 0);
});

test('out-of-travel fader values clamp to the range endpoints', () => {
	const { pitchRatioFromFaderValue } = mapping;
	assert.equal(pitchRatioFromFaderValue(1.4, 8), 1.08);
	assert.equal(pitchRatioFromFaderValue(-0.4, 8), 0.92);
});

test('ratio -> fader-value -> ratio round-trips for values within the selected range', () => {
	const { faderValueFromPitchRatio, pitchRatioFromFaderValue } = mapping;
	for (const pitchRangePct of [8, 16, 100]) {
		for (const ratio of [1, 1 - pitchRangePct / 200, 1 + pitchRangePct / 100, 1 + pitchRangePct / 200]) {
			const value = faderValueFromPitchRatio(ratio, pitchRangePct);
			assert.ok(value >= 0 && value <= 1, `value ${value} must stay within fader travel`);
			assert.ok(
				Math.abs(pitchRatioFromFaderValue(value, pitchRangePct) - ratio) < 1e-9,
				`round-trip mismatch for ratio ${ratio} at range +-${pitchRangePct}%`
			);
		}
	}
});

test('a ratio outside the selected range clamps its fader value to the nearer end', () => {
	const { faderValueFromPitchRatio } = mapping;
	assert.equal(faderValueFromPitchRatio(1.5, 8), 1);
	assert.equal(faderValueFromPitchRatio(0.5, 8), 0);
});

test('invalid inputs are rejected rather than silently coerced', () => {
	const { faderValueFromPitchRatio, pitchRatioFromFaderValue } = mapping;
	assert.throws(() => pitchRatioFromFaderValue(0.5, 0), /pitchRangePct/);
	assert.throws(() => pitchRatioFromFaderValue(0.5, -8), /pitchRangePct/);
	assert.throws(() => pitchRatioFromFaderValue(Number.NaN, 8), /value/);
	assert.throws(() => faderValueFromPitchRatio(0, 8), /ratio/);
	assert.throws(() => faderValueFromPitchRatio(-1, 8), /ratio/);
	assert.throws(() => faderValueFromPitchRatio(1, 0), /pitchRangePct/);
});

// ---------------------------------------------------------------------------
// Review thread https://github.com/maintainer/music-dj-tools/pull/945#discussion_r3918252808
// (P2/NON-BLOCKING): PitchFader's tooltip promised "Double-click resets to
// 0%" with no dblclick handler behind it - the two pointerdown events of a
// real double-click instead set the value to wherever it landed. Implemented
// the reset (PitchFader.svelte handleDoubleClick, wired via ondblclick) so
// the control does what its own explainer says, rather than deleting the
// promise. No component-mount harness exists here (see the describe block
// above for the same constraint), so this pins the wiring by source, same
// idiom as jobs-drawer.test.mjs.
describe('PitchFader double-click reset matches its own tooltip', () => {
	const svelte = readFileSync(
		fileURLToPath(
			new URL('../../src/lib/components/rb/deck/PitchFader.svelte', import.meta.url)
		),
		'utf8'
	);

	it('the tooltip still promises a double-click reset', () => {
		assert.match(svelte, /title="[^"]*Double-click resets to 0%\.?"/);
	});

	it('a real dblclick on the fader is wired to a handler', () => {
		assert.match(svelte, /ondblclick=\{handleDoubleClick\}/);
	});

	it('the handler resets to centre (0%), the same value Home/End never target', () => {
		const fn = svelte.slice(
			svelte.indexOf('function handleDoubleClick('),
			svelte.indexOf('function handlePointerDone(')
		);
		assert.match(fn, /_setTempoFromValue\(0\.5\)/, 'centre fader value is 0.5, not 0 or 1');
	});

	// Review thread https://github.com/maintainer/music-dj-tools/pull/945#discussion_r3919503749
	// (P2/NON-BLOCKING, found on the reset above): both pointerdowns that make
	// up a real double-click already dispatch their own tempo command before
	// this handler runs. Gating the reset on `pending` (the same guard
	// handlePointerDown and handleKeyDown use to refuse STARTING a new
	// gesture while one is in flight) let an in-flight command from the
	// second click win the race and silently drop the reset - worst exactly
	// when the engine is busy. The reset must supersede rather than be
	// dropped, so it carries no such guard.
	it('the reset is not gated on pending, unlike gesture-start handlers', () => {
		const fn = svelte.slice(
			svelte.indexOf('function handleDoubleClick('),
			svelte.indexOf('function handlePointerDone(')
		);
		assert.equal(
			/if \(pending\) return;/.test(fn),
			false,
			'a pending guard here lets an in-flight click command drop the reset'
		);
	});
});

// ---------------------------------------------------------------------------
// Pin 5a5c3b8033d8: the channel meter is intentionally discrete so it has a
// readable green/yellow/red status rather than implying a smooth calibration.
// Source-level coverage is appropriate here because this node:test suite does
// not mount Svelte components, like the PitchFader wiring coverage above.
describe('ChannelLevelMeter discrete live channel display', () => {
	const meter = readFileSync(
		fileURLToPath(
			new URL('../../src/lib/components/rb/mixer/ChannelLevelMeter.svelte', import.meta.url)
		),
		'utf8'
	);
	const fader = readFileSync(
		fileURLToPath(new URL('../../src/lib/components/rb/mixer/VFader.svelte', import.meta.url)),
		'utf8'
	);

	it('renders exactly ten segments split into six green, two yellow, and two red', () => {
		const segments = meter.match(/^\s*'(green|yellow|red)'/gm) ?? [];
		assert.equal(segments.length, 10);
		assert.equal(segments.filter((segment) => segment.includes('green')).length, 6);
		assert.equal(segments.filter((segment) => segment.includes('yellow')).length, 2);
		assert.equal(segments.filter((segment) => segment.includes('red')).length, 2);
	});

	it('keeps real analyser sampling in the extracted component and does not use a gradient', () => {
		assert.match(meter, /peekDeckMeter\(deckId\)/);
		assert.match(meter, /requestAnimationFrame\(tick\)/);
		assert.doesNotMatch(meter, /linear-gradient/);
		assert.match(fader, /import ChannelLevelMeter from '\.\/ChannelLevelMeter\.svelte'/);
		assert.match(fader, /<ChannelLevelMeter \{deckId\} \{playing\} \/>/);
		assert.doesNotMatch(fader, /peekDeckMeter/);
	});

	it('accepts exactly the deck identifier type supported by the meter API', () => {
		assert.match(meter, /deckId: Parameters<typeof peekDeckMeter>\[0\]/);
		assert.doesNotMatch(meter, /from '\$lib\/rb\/deck-slots'/);
	});
});
