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
 * - if any rotating mark reaches inside the r=40 wheel face then broken
 */

let pqtzBarPhase,
	DEFAULT_PQTZ_BAR_BEATS,
	phaseBeatMarks,
	jogPhaseBeats,
	JOG_WHEEL_FACE_RADIUS,
	PHASE_MARK_INNER_RADIUS,
	PHASE_MARK_OUTER_RADIUS;

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

before(async () => {
	({
		pqtzBarPhase,
		DEFAULT_PQTZ_BAR_BEATS,
		phaseBeatMarks,
		jogPhaseBeats,
		JOG_WHEEL_FACE_RADIUS,
		PHASE_MARK_INNER_RADIUS,
		PHASE_MARK_OUTER_RADIUS
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

test('no spinning UI reaches inside the central wheel face', () => {
	assert.equal(JOG_WHEEL_FACE_RADIUS, 40, 'the wheel face is the r=40 disc carrying the BPM text');
	assert.ok(
		PHASE_MARK_INNER_RADIUS > JOG_WHEEL_FACE_RADIUS,
		`a phase mark must stop outside the wheel face - inner radius ${PHASE_MARK_INNER_RADIUS} vs face ${JOG_WHEEL_FACE_RADIUS}`
	);
	assert.ok(
		PHASE_MARK_OUTER_RADIUS > PHASE_MARK_INNER_RADIUS,
		'a mark must have positive length outward from its inner end'
	);

	// The rotating red position tick is spinning UI too: it ran to y=12
	// (radius 38, inside the face) before this pin.
	const redTick = jogDialSource.match(/y1="(\d+)"\s*x2="50"\s*y2="(\d+)"\s*stroke="#d0342c"/);
	assert.ok(redTick !== null, 'the rotating position tick must still exist');
	const deepestRadius = 50 - Number(redTick[2]);
	assert.ok(
		deepestRadius >= JOG_WHEEL_FACE_RADIUS,
		`the rotating position tick reaches radius ${deepestRadius}, inside the r=${JOG_WHEEL_FACE_RADIUS} wheel face`
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
