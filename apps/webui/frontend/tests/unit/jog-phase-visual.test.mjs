import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * Pin 67a4ce88805f (JogDial.svelte) - make a playing deck obviously playing:
 *
 * - a second, fast white line on the wheel that completes one full
 *   revolution per "phase" (a PQTZ bar - 4 beats by default)
 * - the wheel background flips to off-white with black/grey text while
 *   audible, so a playing deck reads unmistakably differently from a
 *   stopped one
 * - a rotating radial grid on that off-white background, divided into as
 *   many sections as the phase tracks (4 by default), completing one
 *   rotation per phase
 * - "phase in config is anchor but set to default 4 if phase not
 *   available/set": the bar length is read from the deck's own
 *   quantize_grid_beats (1/4/8), because that is the one real,
 *   already-plumbed per-deck "beats per phase" setting in this codebase
 *   (deck-state-types.ts QuantizeGrid) - falling back to 4 only when its
 *   value is the unimplemented 'phase' sentinel (never a bare "4" with no
 *   config read at all).
 *
 * Recovered from the closed, never-merged PR #1109 (single commit
 * 11de2243c, "show jog phase while audible") after the stacked branch it
 * was based on was rebuilt from scratch under it and PR #1109 died with its
 * dead base branch. Reimplemented against main's current JogDial (which by
 * pin 27f889893790's sibling packet may also carry a jog progress trail -
 * this pin touches only the background/grid/phase-line, not the trail).
 *
 * SOL REVIEW ROUND (PR #1355, 8bb6d240a): the first cut compared PQTZ's own
 * repeating beat number `n` (1..4, reset every rekordbox bar) directly
 * against barBeats. That made an 8-beat phase snap BACKWARDS every 4 real
 * beats instead of completing one revolution, and made a 1-beat phase null
 * on 3 of every 4 beats. Fixed by deriving phase from the beat's SEQUENTIAL
 * index in the ordered array (`index % barBeats`), which has no reset. The
 * radial grid was also two hardcoded perpendicular lines - always 4
 * sections regardless of barBeats, and the old test only asserted the grid
 * CLASS existed, so a non-4 barBeats could never fail it. Geometry is now
 * `phaseGridSpokes(barBeats)` (wave-math.ts), pure and unit-tested directly
 * for count/endpoints at barBeats 1/4/8, wired into JogDial via {#each}.
 *
 * Regression lines:
 * - if the phase line/grid render while the deck is not audible then broken
 *   (a stopped deck must not look like it is playing)
 * - if the phase angle advances from a CSS/JS wall clock instead of the real
 *   presented position then broken
 * - if the radial grid always divides into a bare literal 4 with no read of
 *   the deck's own quantize grid then broken
 * - if selecting quantize grid 8 does not change the number of radial
 *   sections then broken
 * - if phase is keyed on PQTZ's repeating beat number instead of a
 *   non-repeating sequential index then broken - it will snap backwards
 *   every 4 beats at barBeats=8, and go null on 3/4 beats at barBeats=1
 * - if the wheel background does not flip off-white while playing then broken
 */

let pqtzBarPhase, DEFAULT_PQTZ_BAR_BEATS, phaseGridSpokes;

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

function assertClose(actual, expected, msg) {
	assert.ok(
		Math.abs(actual - expected) < 1e-9,
		`${msg}: expected ${expected}, got ${actual}`
	);
}

before(async () => {
	({ pqtzBarPhase, DEFAULT_PQTZ_BAR_BEATS, phaseGridSpokes } = await loadTypeScriptModule(
		'src/lib/components/rb/wave/wave-math.ts'
	));
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
	// t=1.789 lands in the enclosing beat n=4 (real index 3): phase 0.4375
	// of the 8-beat window. t=2.4 lands in the NEXT real beat, n=1 (real
	// index 4) - PQTZ's own numbering resets to 1 here, exactly the point
	// a naive `beat.n`-keyed implementation snaps backwards. The sequential
	// index has no such reset, so the correct phase keeps climbing.
	const before8 = pqtzBarPhase(REAL_PQTZ_BEATS, 1.789, 8);
	const after8 = pqtzBarPhase(REAL_PQTZ_BEATS, 2.4, 8);
	assertClose(before8, 3.5 / 8, 'phase just before the PQTZ bar-number reset');
	assertClose(after8, 4.7944915254237288 / 8, 'phase just after the PQTZ bar-number reset');
	assert.ok(
		after8 > before8,
		`phase must keep climbing across the bar-number reset (n resets to 1 but the sequential index does not) - got ${before8} then ${after8}`
	);
});

test('pqtzBarPhase at barBeats=1 is defined on every beat, not just the downbeat', () => {
	// n=3 here (not the downbeat) - a `beat.n <= barBeats` gate would reject
	// this and return null on 3 of every 4 beats at barBeats=1.
	assertClose(pqtzBarPhase(REAL_PQTZ_BEATS, 1.3165, 1), 0.5, 'phase within the single enclosing beat');
});

test('pqtzBarPhase honours a caller-supplied bar length (the quantize-grid anchor)', () => {
	assert.throws(() => pqtzBarPhase(REAL_PQTZ_BEATS, 1.3165, 0), RangeError);
	assert.throws(() => pqtzBarPhase(REAL_PQTZ_BEATS, 1.3165, 1.5), RangeError);
});

test('phaseGridSpokes generates exactly barBeats spokes, evenly spaced, ending on the wheel', () => {
	assert.equal(typeof phaseGridSpokes, 'function');

	const one = phaseGridSpokes(1);
	assert.equal(one.length, 1);
	assertClose(one[0].angleDeg, 0);
	assertClose(one[0].x2, 50);
	assertClose(one[0].y2, 12);

	const four = phaseGridSpokes(4);
	assert.equal(four.length, 4, 'default phase must divide the wheel into 4 sections');
	assert.deepEqual(
		four.map((s) => s.angleDeg),
		[0, 90, 180, 270]
	);
	// Must match the OLD hardcoded cross's reach exactly (y=12/88, x=12/88)
	// so the default-4 case is a pure geometry refactor, not a visual change.
	assertClose(four[0].x2, 50);
	assertClose(four[0].y2, 12);
	assertClose(four[1].x2, 88);
	assertClose(four[1].y2, 50);
	assertClose(four[2].x2, 50);
	assertClose(four[2].y2, 88);
	assertClose(four[3].x2, 12);
	assertClose(four[3].y2, 50);

	const eight = phaseGridSpokes(8);
	assert.equal(eight.length, 8, 'selecting an 8-beat quantize grid must render 8 sections, not 4');
	assert.deepEqual(
		eight.map((s) => s.angleDeg),
		[0, 45, 90, 135, 180, 225, 270, 315]
	);

	assert.throws(() => phaseGridSpokes(0), RangeError);
	assert.throws(() => phaseGridSpokes(1.5), RangeError);
});

test('JogDial wires the phase visual to quantize_grid_beats, defaulting to 4 only for the unimplemented phase sentinel', () => {
	const filename = new URL('../../src/lib/components/rb/deck/JogDial.svelte', import.meta.url);
	const source = readFileSync(filename, 'utf8');

	assert.match(source, /import \{[^}]*pqtzBarPhase[^}]*phaseGridSpokes[^}]*\} from '\$lib\/components\/rb\/wave\/wave-math';/);
	assert.match(
		source,
		/deck\.quantize_grid_beats === 'phase' \? DEFAULT_PQTZ_BAR_BEATS : deck\.quantize_grid_beats/,
		'the bar length must come from the deck\'s own config, defaulting to 4 only when that config is the unimplemented phase sentinel'
	);
	assert.match(source, /pqtzBarPhase\(deck\.anlz\?\.beatgrid\.beats \?\? \[\], Math\.max\(0, deck\.position_ms \/ 1000\), barBeats\)/);
	assert.match(source, /const gridSpokes[^=]*=\s*\$derived\(phaseGridSpokes\(barBeats\)\)/);
	assert.match(
		source,
		/\{#each gridSpokes as spoke[^}]*\}[\s\S]{0,200}<line[^>]*x2=\{spoke\.x2\}[^>]*y2=\{spoke\.y2\}/,
		'the grid must render one line per spoke computed FROM barBeats, not a hardcoded pair'
	);
	assert.doesNotMatch(
		source,
		/<line x1="50" y1="12" x2="50" y2="88" \/>\s*<line x1="12" y1="50" x2="88" y2="50" \/>/,
		'the old hardcoded 4-section cross must be gone - section count has to come from barBeats'
	);
	assert.match(source, /class:dial-playing=\{deck\.audible\}/);
	assert.match(source, /\{#if deck\.audible\}[\s\S]*class="playing-phase-grid"/);
	assert.match(source, /class="phase-marker"/);
	assert.doesNotMatch(source, /animation:/, 'phase must not advance from a CSS wall clock');
});
