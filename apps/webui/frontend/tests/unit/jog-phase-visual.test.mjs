import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * Pin 67a4ce88805f (JogDial.svelte) made a playing deck obviously playing: an
 * off-white wheel face while audible, plus a fast white line completing one
 * full revolution per "phase" (a PQTZ bar).
 *
 * Pin f19a1b2a455a (the maintainer, Tue 8 Sep 2026) rejects what that first cut
 * actually shipped, verbatim:
 *
 *   "ok remov spinning black line - looks bad, the white line is plenty -
 *    but it's regressed, it should spin once per phase (4 beats default) and
 *    have as many marks are there beats per phase ie 4 if 4. downbeat or
 *    beat1 should be thicker and others thinner. no spinning UI to overlap
 *    the central wheel now."
 *
 * Four requirements, and the regression that produced them:
 *
 * 1. The rotating radial grid (`.playing-phase-grid`, stroke #1f2329) is the
 *    "spinning black line". It is gone. Only white marks remain.
 * 2. One revolution per phase, 4 beats by DEFAULT. The regression: barBeats
 *    was read straight off `deck.quantize_grid_beats`, whose shipped default
 *    is 1 (player/state.svelte.ts, landed in bb606116c #1270), so on an
 *    untouched deck the visual completed a revolution every single BEAT.
 *    `jogPhaseBeats` resolves 1 and the unimplemented 'phase' sentinel to
 *    DEFAULT_PQTZ_BAR_BEATS while still honouring a chosen 4 or 8.
 * 3. As many marks as beats per phase, beat 1 (the downbeat) thicker.
 *    The first cut drew exactly ONE white marker regardless of barBeats.
 * 4. No spinning UI over the central wheel. The black spokes ran from the
 *    dial CENTRE outward; the white marker and the red position tick both
 *    ran to y=12, i.e. radius 38, inside the r=40 wheel face.
 *
 * Regression lines:
 * - if the phase visual renders while the deck is not audible then broken
 * - if the phase angle advances from a CSS/JS wall clock instead of the real
 *   presented position then broken
 * - if any black/dark spinning grid returns to the wheel then broken
 * - if a default deck (quantize grid 1) does not spin once per 4 beats with
 *   4 marks then broken
 * - if the marks do not number exactly barBeats then broken
 * - if beat 1's mark is not strictly thicker than the others then broken
 * - if any rotating mark RENDERS inside the wheel face (its own stroke
 *   included on both sides) then broken
 * - if the red position tick stops sharing the marks' annulus then broken
 *
 * That last line is deliberately about rendered extent rather than
 * centerlines. The first cut of this pin asserted only the endpoint radii,
 * and passed while the stroke-width-3 downbeat, drawn with a round line cap,
 * put 1.5 units of white 0.5 units inside the face (Sol, thread 3961741980).
 * A round cap is a semicircle of radius strokeWidth/2 centred on the
 * endpoint, so a RADIAL mark reaches strokeWidth/2 further in than its inner
 * endpoint - and the same arithmetic applies to the round-capped red
 * position tick, which is stroke-width 3 and rotates.
 */

let pqtzBarPhase,
	DEFAULT_PQTZ_BAR_BEATS,
	phaseBeatMarks,
	jogPhaseBeats,
	JOG_WHEEL_FACE_RADIUS,
	PHASE_MARK_INNER_RADIUS,
	PHASE_MARK_OUTER_RADIUS,
	PHASE_MARK_STROKE_WIDTH,
	PHASE_DOWNBEAT_STROKE_WIDTH,
	POSITION_TICK_STROKE_WIDTH;

const REAL_PQTZ_BEATS = [
	{ n: 1, bpm: 127, t: 0.135 },
	{ n: 2, bpm: 127, t: 0.608 },
	{ n: 3, bpm: 127, t: 1.08 },
	{ n: 4, bpm: 127, t: 1.553 },
	{ n: 1, bpm: 127, t: 2.025 },
	{ n: 2, bpm: 127, t: 2.497 },
	{ n: 3, bpm: 127, t: 2.97 },
	{ n: 4, bpm: 127, t: 3.442 }
];

const jogDialSource = readFileSync(
	new URL('../../src/lib/components/rb/deck/JogDial.svelte', import.meta.url),
	'utf8'
);

function assertClose(actual, expected, msg) {
	assert.ok(Math.abs(actual - expected) < 1e-9, `${msg}: expected ${expected}, got ${actual}`);
}

/** stroke-width declared for one CSS selector in JogDial's <style> block. */
function strokeWidthOf(selector) {
	const escaped = selector.replace(/[.]/g, '\\.');
	const rule = new RegExp(`${escaped}\\s*\\{[^}]*?stroke-width:\\s*([0-9.]+)`);
	const found = jogDialSource.match(rule);
	assert.ok(found !== null, `no stroke-width found for ${selector}`);
	return Number(found[1]);
}

/** The whole <line> element of the rotating red position tick, found by the
 * colour it is drawn in so this survives a rename of its class. */
function positionTickElement() {
	const found = jogDialSource.match(/<line[^>]*stroke="#d0342c"[^>]*\/>/);
	assert.ok(found !== null, 'the rotating position tick must still exist');
	return found[0];
}

/** stroke-width declared as an attribute on one SVG element. */
function attrStrokeWidth(element, what) {
	const found = element.match(/stroke-width="([0-9.]+)"/);
	assert.ok(found !== null, `${what} must declare a stroke-width`);
	return Number(found[1]);
}

/** The inner-endpoint radius of a radial element drawn at x=50 in the
 * 100x100 viewBox: either a literal y2 or the shared markInnerY binding.
 * This is the CENTERLINE radius - the rendered edge is half a stroke
 * further in again, which is the entire point of the test below. */
function innerEndpointRadius(element, what) {
	const found = element.match(/y2=(?:"([0-9.]+)"|\{markInnerY\})/);
	assert.ok(found !== null, `${what} must end at a literal y2 or the exported markInnerY`);
	return found[1] === undefined ? PHASE_MARK_INNER_RADIUS : 50 - Number(found[1]);
}

/** Round line caps are what make rendered extent differ from centerline. */
function assertRoundCapped(source, what) {
	assert.match(
		source,
		/stroke-linecap:\s*round|stroke-linecap="round"/,
		`${what} is assumed round-capped by the clearance arithmetic; a different cap changes the geometry`
	);
}

before(async () => {
	({
		pqtzBarPhase,
		DEFAULT_PQTZ_BAR_BEATS,
		phaseBeatMarks,
		jogPhaseBeats,
		JOG_WHEEL_FACE_RADIUS,
		PHASE_MARK_INNER_RADIUS,
		PHASE_MARK_OUTER_RADIUS,
		PHASE_MARK_STROKE_WIDTH,
		PHASE_DOWNBEAT_STROKE_WIDTH,
		POSITION_TICK_STROKE_WIDTH
	} = await loadTypeScriptModule('src/lib/components/rb/wave/wave-math.ts'));
});

test('pqtzBarPhase reads real captured beat numbers/timings, never a wall clock', () => {
	assert.equal(typeof pqtzBarPhase, 'function');
	assert.equal(DEFAULT_PQTZ_BAR_BEATS, 4);
	assert.equal(pqtzBarPhase(REAL_PQTZ_BEATS, 1.3165), 0.625);
	assert.equal(pqtzBarPhase(REAL_PQTZ_BEATS, 1.789), 0.875);
	assert.equal(pqtzBarPhase(REAL_PQTZ_BEATS, 3.443), null, 'no following beat means no invented phase');
	assert.equal(pqtzBarPhase([], 1), null, 'a missing PQTZ grid parks the visual at its downbeat');
});

test('pqtzBarPhase at barBeats=8 keeps ramping across a bar reset instead of snapping backwards', () => {
	const before8 = pqtzBarPhase(REAL_PQTZ_BEATS, 1.789, 8);
	const after8 = pqtzBarPhase(REAL_PQTZ_BEATS, 2.4, 8);
	assertClose(before8, 3.5 / 8, 'phase just before the PQTZ bar-number reset');
	assertClose(after8, 4.7944915254237288 / 8, 'phase just after the PQTZ bar-number reset');
	assert.ok(after8 > before8, `phase must keep climbing across the bar-number reset - got ${before8} then ${after8}`);
});

test('pqtzBarPhase at barBeats=1 is defined on every beat, not just the downbeat', () => {
	assertClose(pqtzBarPhase(REAL_PQTZ_BEATS, 1.3165, 1), 0.5, 'phase within the single enclosing beat');
});

test('pqtzBarPhase honours a caller-supplied bar length (the quantize-grid anchor)', () => {
	assert.throws(() => pqtzBarPhase(REAL_PQTZ_BEATS, 1.3165, 0), RangeError);
	assert.throws(() => pqtzBarPhase(REAL_PQTZ_BEATS, 1.3165, 1.5), RangeError);
});

test('a deck at its SHIPPED default quantize grid spins once per 4 beats, not once per beat', () => {
	// Read the real seeded default rather than restating "1", so this test
	// keeps describing the app if that seed ever moves.
	const stateSource = readFileSync(
		new URL('../../src/lib/player/state.svelte.ts', import.meta.url),
		'utf8'
	);
	const seeded = stateSource.match(/quantize_grid_beats:\s*('phase'|\d+)/);
	assert.ok(seeded !== null, 'player state must seed a quantize grid for a fresh deck');
	const seededGrid = seeded[1] === "'phase'" ? 'phase' : Number(seeded[1]);

	assert.equal(typeof jogPhaseBeats, 'function');
	assert.equal(
		jogPhaseBeats(seededGrid),
		4,
		`a fresh deck (quantize grid ${seeded[1]}) must spin once per 4-beat phase, not once per beat`
	);
	assert.equal(jogPhaseBeats('phase'), 4, "the unimplemented 'phase' sentinel is not a phase length");
	assert.equal(jogPhaseBeats(4), 4, 'a deliberately chosen 4-beat grid still anchors the phase');
	assert.equal(jogPhaseBeats(8), 8, 'a deliberately chosen 8-beat grid still anchors the phase');
	assert.throws(() => jogPhaseBeats(0), RangeError);
	assert.throws(() => jogPhaseBeats(1.5), RangeError);
});

test('phaseBeatMarks yields one mark per beat in the phase, only beat 1 flagged downbeat', () => {
	assert.equal(typeof phaseBeatMarks, 'function');

	const four = phaseBeatMarks(4);
	assert.equal(four.length, 4, 'a 4-beat phase carries 4 marks');
	assert.deepEqual(four.map((m) => m.angleDeg), [0, 90, 180, 270]);
	assert.deepEqual(
		four.map((m) => m.isDownbeat),
		[true, false, false, false],
		'exactly beat 1 is the downbeat'
	);

	const eight = phaseBeatMarks(8);
	assert.equal(eight.length, 8, 'an 8-beat phase carries 8 marks');
	assert.deepEqual(eight.map((m) => m.angleDeg), [0, 45, 90, 135, 180, 225, 270, 315]);
	assert.equal(eight.filter((m) => m.isDownbeat).length, 1);

	assert.equal(phaseBeatMarks(1).length, 1);
	assert.throws(() => phaseBeatMarks(0), RangeError);
	assert.throws(() => phaseBeatMarks(1.5), RangeError);
});

test('no spinning UI RENDERS inside the central wheel face, round caps included', () => {
	assert.equal(JOG_WHEEL_FACE_RADIUS, 40, 'the wheel face is the r=40 disc carrying the BPM text');
	assert.ok(
		PHASE_MARK_OUTER_RADIUS > PHASE_MARK_INNER_RADIUS,
		'a mark must have positive length outward from its inner end'
	);

	// The face is stroked too, so its own rendered edge - not the geometric
	// r=40 - is what a mark has to clear. Read that width from the component
	// as well, so neither side of the comparison is a number nobody draws.
	const face = jogDialSource.match(/<circle\s+class="wheel-fill"[^>]*\/>/);
	assert.ok(face !== null, 'the wheel face must still be drawn');
	assert.match(face[0], /r=\{JOG_WHEEL_FACE_RADIUS\}/, 'the face radius must BE the exported constant');
	const faceRenderedRadius =
		JOG_WHEEL_FACE_RADIUS + attrStrokeWidth(face[0], 'the wheel face') / 2;

	const tick = positionTickElement();
	const markRule = jogDialSource.match(/\.phase-mark\s*\{[^}]*\}/);
	assert.ok(markRule !== null, 'the phase marks must carry a style rule');
	assertRoundCapped(markRule[0], 'the phase marks');
	assertRoundCapped(tick, 'the red position tick');

	// Widths come from where they are actually RENDERED - JogDial's own CSS
	// and attributes - so this cannot be satisfied by a constant in
	// wave-math that the component does not use.
	const rotating = [
		['thin beat mark', strokeWidthOf('.phase-mark'), PHASE_MARK_INNER_RADIUS],
		['thick downbeat mark', strokeWidthOf('.phase-mark.downbeat'), PHASE_MARK_INNER_RADIUS],
		['red position tick', attrStrokeWidth(tick, 'the red position tick'), innerEndpointRadius(tick, 'the red position tick')]
	];

	// Report EVERY offender rather than stopping at the first: the thick
	// downbeat and the red position tick are the same defect, and fixing
	// only whichever assertion fired first would leave the other overlapping.
	const overlapping = rotating
		.map(([what, strokeWidth, centerlineRadius]) => ({
			what,
			strokeWidth,
			centerlineRadius,
			renderedRadius: centerlineRadius - strokeWidth / 2
		}))
		.filter((mark) => !(mark.renderedRadius > faceRenderedRadius));

	assert.deepEqual(
		overlapping.map((mark) => mark.what),
		[],
		overlapping
			.map(
				(mark) =>
					`the ${mark.what} is stroke-width ${mark.strokeWidth} with a round line cap, so its inner endpoint ` +
					`at radius ${mark.centerlineRadius} RENDERS down to radius ${mark.renderedRadius} - inside the ` +
					`wheel face, which itself renders out to r=${faceRenderedRadius}. Comparing the centerline alone ` +
					`(${mark.centerlineRadius} > ${JOG_WHEEL_FACE_RADIUS}) hides this.`
			)
			.join('\n')
	);
});

test('the inner radius is derived from the widths JogDial really draws, not a guess', () => {
	// The clearance arithmetic in wave-math is only correct while its stroke
	// constants match the component. Without this, someone thickening the
	// downbeat in CSS alone would re-open the overlap silently.
	assert.equal(strokeWidthOf('.phase-mark'), PHASE_MARK_STROKE_WIDTH);
	assert.equal(strokeWidthOf('.phase-mark.downbeat'), PHASE_DOWNBEAT_STROKE_WIDTH);
	assert.equal(attrStrokeWidth(positionTickElement(), 'the red position tick'), POSITION_TICK_STROKE_WIDTH);

	// The tick shares the marks' annulus rather than carrying its own
	// literal endpoint, which is how it came to reach r=38 and then r=39.5.
	assert.match(
		positionTickElement(),
		/y1=\{markOuterY\}[\s\S]*y2=\{markInnerY\}/,
		'the rotating position tick must take its endpoints from the exported radii'
	);
});

test('JogDial renders white per-beat marks and no black spinning grid', () => {
	assert.doesNotMatch(
		jogDialSource,
		/playing-phase-grid|grid-spoke|phaseGridSpokes/,
		'the spinning black radial grid must be gone entirely - "the white line is plenty"'
	);
	assert.doesNotMatch(
		jogDialSource,
		/x1="50"\s*y1="50"/,
		'nothing rotating may be drawn from the dial centre outward'
	);
	assert.match(
		jogDialSource,
		/const barBeats: number = \$derived\(jogPhaseBeats\(deck\.quantize_grid_beats\)\)/,
		'phase length must go through jogPhaseBeats, not straight off the raw quantize grid'
	);
	assert.doesNotMatch(
		jogDialSource,
		/deck\.quantize_grid_beats === 'phase' \? DEFAULT_PQTZ_BAR_BEATS : deck\.quantize_grid_beats/,
		'the old raw read defaulted a fresh deck to a 1-beat phase'
	);
	assert.match(jogDialSource, /const phaseMarks: PhaseBeatMark\[\] = \$derived\(phaseBeatMarks\(barBeats\)\)/);
	assert.match(
		jogDialSource,
		/\{#each phaseMarks as mark[^}]*\}[\s\S]{0,400}class:downbeat=\{mark\.isDownbeat\}/,
		'one line per mark, with beat 1 flagged for the thicker style'
	);
	assert.match(
		jogDialSource,
		/y1=\{markOuterY\}[\s\S]{0,80}y2=\{markInnerY\}/,
		'mark endpoints must come from the exported radii, not inline literals'
	);
	assert.match(jogDialSource, /\{#if deck\.audible\}[\s\S]{0,200}class="phase-marks"/);
	assert.match(
		jogDialSource,
		/class="wheel-fill"[^>]*r=\{JOG_WHEEL_FACE_RADIUS\}/,
		'the face the marks must stay outside of has to BE the exported radius, or the no-overlap arithmetic is comparing against a literal nobody draws'
	);
	assert.match(jogDialSource, /class:dial-playing=\{deck\.audible\}/);
	assert.doesNotMatch(jogDialSource, /animation:/, 'phase must not advance from a CSS wall clock');
});

test('beat 1 is drawn strictly thicker than the other beats, and all marks are white', () => {
	const base = strokeWidthOf('.phase-mark');
	const downbeat = strokeWidthOf('.phase-mark.downbeat');
	assert.ok(
		downbeat > base,
		`the downbeat mark must be thicker than the rest - got downbeat ${downbeat} vs ${base}`
	);
	assert.match(
		jogDialSource,
		/\.phase-mark\s*\{[^}]*stroke:\s*#fff/,
		'the phase marks are the white line the pin kept'
	);
});
