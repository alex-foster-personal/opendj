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
// Review thread https://github.com/private_owner/music-dj-tools/pull/945#discussion_r3917231716
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
// Review thread https://github.com/private_owner/music-dj-tools/pull/945#discussion_r3918252808
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

	// Review thread https://github.com/private_owner/music-dj-tools/pull/945#discussion_r3919503749
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

// issue: 186
// Empty-deck tempo refuse: MIDI (action-glue deck_pitch) and the mouse-wheel
// path already refuse when no track is loaded. Pointer, keyboard, and
// double-click must match that contract instead of dispatching tempo and
// letting the engine throw. Range buttons stay live: setPitchRange does not
// require a loaded track. Source-scanned because this suite does not mount
// Svelte components (same idiom as the double-click block above).
describe('PitchFader empty-deck tempo refuse (issue 186)', () => {
	const svelte = readFileSync(
		fileURLToPath(
			new URL('../../src/lib/components/rb/deck/PitchFader.svelte', import.meta.url)
		),
		'utf8'
	);
	const deckSrc = readFileSync(
		fileURLToPath(new URL('../../src/lib/components/rb/Deck.svelte', import.meta.url)),
		'utf8'
	);

	it('pointer and keyboard gesture starts refuse when no track is loaded', () => {
		const pointer = svelte.slice(
			svelte.indexOf('function handlePointerDown('),
			svelte.indexOf('function handlePointerMove(')
		);
		const key = svelte.slice(
			svelte.indexOf('function handleKeyDown('),
			svelte.indexOf('</script>')
		);
		assert.match(
			pointer,
			/if \(pending \|\| deck\.stable_id === null\) return;/,
			'handlePointerDown must treat no-track like pending'
		);
		assert.match(
			key,
			/if \(pending \|\| deck\.stable_id === null\) return;/,
			'handleKeyDown must treat no-track like pending'
		);
	});

	it('double-click refuses an empty deck but still has no pending guard', () => {
		const fn = svelte.slice(
			svelte.indexOf('function handleDoubleClick('),
			svelte.indexOf('function handlePointerDone(')
		);
		assert.match(fn, /if \(deck\.stable_id === null\) return;/);
		assert.equal(
			/if \(pending\) return;/.test(fn),
			false,
			'a pending guard here lets an in-flight click command drop the reset'
		);
	});

	it('range buttons stay live without a track', () => {
		const loop = svelte.slice(svelte.indexOf('{#each PITCH_RANGES'), svelte.indexOf('{/each}'));
		assert.match(loop, /onclick=\{async \(\) => await onRangeChange\(range\)\}/);
		assert.match(loop, /disabled=\{pending\}/);
		assert.equal(
			/stable_id/.test(loop),
			false,
			'range switcher must not require a loaded track'
		);
	});

	it('Deck mounts PitchFader onto setTempo / setPitchRangeUi and resets first on narrow', () => {
		assert.match(
			deckSrc,
			/<PitchFader[\s\S]*onTempoChange=\{setTempo\}[\s\S]*onRangeChange=\{setPitchRangeUi\}/
		);
		const fn = deckSrc.slice(
			deckSrc.indexOf('async function setPitchRangeUi('),
			deckSrc.indexOf('async function unloadDeck(')
		);
		assert.match(fn, /type: 'tempo'/);
		assert.match(fn, /ratio: 1/);
		assert.match(fn, /type: 'pitch_range'/);
		assert.match(fn, /pitchRange/);
		assert.match(fn, /deck\.pitch/);
		assert.equal(fn.includes('16'), false, 'range-narrow compare must not hardcode 16');
	});

	it('AX labels and test ids on the fader and range buttons are unchanged', () => {
		assert.match(svelte, /aria-label=\{`deck \$\{deck\.deck_id\} pitch fader`\}/);
		assert.match(svelte, /data-testid=\{`pitch-fader-deck-\$\{deck\.deck_id\}`\}/);
		assert.match(svelte, /data-testid=\{`pitch-range-\$\{range\}-deck-\$\{deck\.deck_id\}`\}/);
		assert.match(
			svelte,
			/aria-label=\{`pitch range \$\{range === 100 \? 'wide' : `\$\{range\} percent`\} deck \$\{deck\.deck_id\}`\}/
		);
		assert.match(svelte, /aria-disabled=\{pending \|\| deck\.stable_id === null\}/);
		assert.match(svelte, /tabindex="0"/);
		assert.match(svelte, /data-performance-control="pitch"/);
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

	// The band split moved from a hardcoded 6/2/2 array in this component to
	// meter-math's dB thresholds, because the old lighting rule was
	// `ceil(meter * 10)` against a LINEAR amplitude: real music sits far below
	// full scale in linear terms, so the top segments were unreachable and the
	// bar behaved like an on/off lamp. The count is pinned here; WHICH band each
	// segment carries is pinned in meter-math.test.mjs, where it can be checked
	// against the dB scale that decides it.
	// #1475: thresholds now route through segmentThresholdsForRed(), which
	// returns the unmodified SEGMENT_THRESHOLDS_DBFS when calibration is off -
	// this component still owns none of the shift/band policy itself.
	it('renders ten segments and owns none of the policy that colors them', () => {
		assert.match(meter, /segmentThresholdsForRed\(/);
		assert.match(meter, /thresholds\.map/);
		assert.match(meter, /segmentBand\(index \+ 1\)/);
		// If a threshold or a band name is ever pasted back into this component,
		// the single source of truth has forked and this catches it.
		assert.doesNotMatch(meter, /^\s*'(green|amber|yellow|red)',/m);
		for (const threshold of ['-34', '-26', '-20', '-16', '-12', '-9', '-6', '-3']) {
			assert.ok(
				!meter.includes(`${threshold},`),
				`ChannelLevelMeter hardcodes the dB threshold ${threshold}`
			);
		}
	});

	it('lights segments from a dB reading, never from a linear amplitude', () => {
		// The exact regression: `ceil(meter * SEGMENTS.length)` is linear and
		// must not come back.
		assert.doesNotMatch(meter, /Math\.ceil\(\s*meter\s*\*/);
		// #1475: segments are recomputed from the calibrated thresholds rather
		// than trusted from meter-tap's `reading.segments`, which only ever
		// knows the uncalibrated default scale (meter-tap stays deck-agnostic
		// and policy-free by design - see meter-tap.ts's module doc).
		assert.doesNotMatch(meter, /reading\.segments/);
		assert.match(meter, /segmentsLitFromDbfs\(reading\.db, thresholds\)/);
		assert.match(meter, /reading\.db/);
	});

	it('labels the tap position honestly and does not claim speaker risk', () => {
		// The old label said "post-deck, pre-channel-fader" while the underlying
		// tap sat BEFORE trim and EQ, so no mixer control moved it.
		assert.match(meter, /post-EQ post-fader/);
		assert.match(meter, /channel volume fader/);
		assert.match(meter, /not speaker risk/);
		assert.doesNotMatch(meter, /speaker damage|damage risk/i);
		// House rule: a numeric readout says what the number is.
		assert.match(meter, /dBFS/);
	});

	it('feeds the fader meter from the same post-fader AudioNode that setFader changes', () => {
		const graph = readFileSync(
			fileURLToPath(new URL('../../src/lib/rb/deck-channel-graph.ts', import.meta.url)),
			'utf8'
		);
		const engine = readFileSync(
			fileURLToPath(new URL('../../src/lib/rb/audio-engine.svelte.ts', import.meta.url)),
			'utf8'
		);
		assert.match(graph, /meterSources\.push\(\{ tap, source: fader \}\)/);
		assert.match(engine, /_setParam\(nodes\.fader\.gain, value\)/);
		assert.doesNotMatch(graph, /meterSources\.push\(\{ tap, source: high \}\)/);
	});

	// Issue #3529: source matching proves the tap is wired to `fader`, but only
	// a real Web Audio graph test can prove `fader.gain` actually moves the
	// meter reading. See meter-artifact.spec.ts (channel-meter-browser-entry).
	it('documents that post-fader metering needs a real graph test, not source grep alone', () => {
		const artifact = readFileSync(
			fileURLToPath(
				new URL('../../tests/e2e/fixtures/channel-meter-browser-entry.ts', import.meta.url)
			),
			'utf8'
		);
		assert.match(artifact, /buildDeckChannelGraph/);
		assert.match(artifact, /readChannelMeterAtFaderGains/);
		assert.match(artifact, /readChannelMeterFloorsOnNextObservation/);
		assert.match(artifact, /issue #3529/);
	});

	it('keeps real analyser sampling in the extracted component and does not use a gradient', () => {
		assert.match(meter, /peekDeckMeterReading\(deckId\)/);
		assert.match(meter, /requestAnimationFrame\(tick\)/);
		assert.doesNotMatch(meter, /linear-gradient/);
		assert.match(fader, /import ChannelLevelMeter from '\.\/ChannelLevelMeter\.svelte'/);
		assert.match(fader, /<ChannelLevelMeter \{deckId\} \{playing\} \/>/);
		assert.doesNotMatch(fader, /peekDeckMeter/);
	});

	// Codex P2 BLOCKING on #1503. Scoping the clip rule to `.lit` (which stopped
	// a latch painting segments that were not lit) ALSO made the latch invisible
	// the moment the level fell back below red -- exactly the brief overshoot the
	// latch exists to show. The latch now owns a dedicated segment.
	//
	// Regression line: if the latch has no indicator of its own again, a
	// transient clip is unreportable and the latch is decorative.
	it('the clip latch owns a segment rather than repainting the red band', () => {
		assert.match(meter, /class:clip-latch=\{clipped && index === SEGMENTS\.length - 1\}/);
		assert.match(meter, /\.rb-channel-level-meter-segment\.clip-latch\s*\{/);
		// The level-scoped rule must still require .lit, or the original bug returns.
		assert.match(meter, /\.clipped \.rb-channel-level-meter-segment\.red\.lit\s*\{/);
		assert.doesNotMatch(meter, /\.clipped \.rb-channel-level-meter-segment\.red\s*\{/);
	});

	// Codex P1 BLOCKING on #1503: capturing on a stopped deck reads the meter
	// floor, and arming that ceiling applies a 0.001 master multiplier, i.e. one
	// click silences the whole output.
	//
	// Regression line: if the floor guard goes, M on a silent channel mutes the app.
	it('a calibration capture at the meter floor is refused', () => {
		const stripSrc = readFileSync(
			fileURLToPath(new URL('../../src/lib/components/rb/mixer/ChannelStrip.svelte', import.meta.url)),
			'utf8'
		);
		// Transport state FIRST: the meter's PPM ballistics decay at ~11.8 dB/s,
		// so for seconds after a pause the tap still reads a real-looking value
		// on its way down. A numeric floor alone lets that be captured.
		assert.match(stripSrc, /if \(!playing\) return;/);
		assert.match(stripSrc, /if \(db <= METER_FLOOR_DBFS\) return;/);
		assert.match(stripSrc, /import \{ METER_FLOOR_DBFS \} from '\$lib\/rb\/meter-math'/);
		// The refusal must be explained where the user can see it.
		assert.match(stripSrc, /stopped or silent channel captures nothing/i);
	});

	it('accepts exactly the deck identifier type supported by the meter API', () => {
		// Now a direct import: the meter API no longer exposes a single function
		// whose first parameter can stand in for the type.
		assert.match(meter, /deckId: Parameters<typeof peekDeckMeterReading>\[0\]/);
		// Holds an import edge off deck-slots, which sits near the fan-in allowance.
		assert.doesNotMatch(meter, /from '\$lib\/rb\/deck-slots'/);
	});
});
