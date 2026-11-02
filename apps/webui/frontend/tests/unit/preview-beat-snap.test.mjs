/**
 * PVSNAP spike: a preview click lands in phase and on the bar, as if a synced
 * deck had been sent there (src/lib/player/preview-beat-snap.ts).
 *
 * Every assertion is on the MEASURED phase error of the resulting plan
 * (`preview-beat-snap-measure.mjs`, the same function the live harness uses),
 * not on the plan's own fields, so a plan that reports a phase it does not
 * produce still fails. The control at the bottom proves the measurement bites:
 * the pre-snap behavior (start at the click, tempo only) measures far out.
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';
import { constantGrid, phaseErrorAt } from './preview-beat-snap-measure.mjs';

let planPreviewSnap;
let previewStartFor;
let PREVIEW_SNAP_LEAD_SEC;
let gridBeatPosition;

before(async () => {
	({ planPreviewSnap, previewStartFor, PREVIEW_SNAP_LEAD_SEC } = await loadTypeScriptModule(
		'src/lib/player/preview-beat-snap.ts'
	));
	({ gridBeatPosition } = await loadTypeScriptModule('src/lib/rb/beat-sync-math.ts'));
});

const MASTER = constantGrid(600, 128, 0.12);
const PREVIEW = constantGrid(500, 124, 0.31);
const masterBeat = 60 / 128;
/** Master 40% through beat 3 of bar 50: index 50*4 + 2. */
const MASTER_AT = MASTER[202].t + 0.4 * masterBeat;

function request(overrides = {}) {
	return {
		masterGrid: MASTER,
		previewGrid: PREVIEW,
		masterPositionAtStartSec: MASTER_AT,
		masterTempoRatio: 1,
		clickSec: PREVIEW[160].t + 0.05, // just after a preview downbeat
		nowContextSec: 10,
		startAtContextSec: 10.05,
		pitchRangePct: 16,
		mode: 'bar',
		...overrides
	};
}

/** Worst |error| over `bars` bars after the start, sampled 4x per beat. */
function worstOver(req, plan, bars = 8) {
	let worst = 0;
	let allSameNumber = true;
	for (let k = 0; k < bars * 16; k += 1) {
		const at = plan.startAtContextSec + (k * masterBeat) / 4 / req.masterTempoRatio;
		const m = phaseErrorAt({ gridBeatPosition, ...req, plan, atContextSec: at });
		assert.ok(m !== null, `sample ${k} fell off a grid`);
		worst = Math.max(worst, Math.abs(m.errorMs));
		allSameNumber &&= m.sameBeatNumber;
	}
	return { worst, allSameNumber };
}

test('R1 BAR: lands in phase on the master beat number nearest the click', () => {
	const req = request();
	const plan = planPreviewSnap(req);
	assert.equal(plan.ok, true, plan.reason);
	assert.equal(plan.masterBeatNumber, 3);
	assert.equal(PREVIEW[plan.previewBeatIndex].n, 3);
	// Nearest beat-3 to a click just after beat 160 (a downbeat) is 158 or 162.
	assert.ok(Math.abs(plan.previewBeatIndex - 160) <= 2, `index ${plan.previewBeatIndex}`);
	const { worst, allSameNumber } = worstOver(req, plan);
	assert.ok(worst < 0.5, `worst phase error ${worst.toFixed(3)} ms over 8 bars`);
	assert.ok(allSameNumber, 'preview left the master beat number');
});

test('R2 tempo: the rate is the master tempo over the preview tempo', () => {
	const plan = planPreviewSnap(request());
	assert.ok(Math.abs(plan.rate - 128 / 124) < 1e-6, `rate ${plan.rate}`);
});

test('R2 tempo: a pitched master is followed, not its nominal BPM', () => {
	const req = request({ masterTempoRatio: 1.04 });
	const plan = planPreviewSnap(req);
	assert.equal(plan.ok, true, plan.reason);
	assert.ok(Math.abs(plan.rate - (128 * 1.04) / 124) < 1e-6, `rate ${plan.rate}`);
	assert.ok(worstOver(req, plan).worst < 0.5);
});

test('R2 range: a match outside the pitch range is refused with a reason', () => {
	const req = request({ previewGrid: constantGrid(500, 100, 0.31) });
	const plan = planPreviewSnap(req);
	assert.equal(plan.ok, false);
	assert.match(plan.reason, /\S/);
});

test('R2 half time: 87 under a 174 master snaps at its own speed', () => {
	const master = constantGrid(800, 174, 0.05);
	const req = request({
		masterGrid: master,
		previewGrid: constantGrid(400, 87, 0.2),
		masterPositionAtStartSec: master[401].t + 0.25 * (60 / 174)
	});
	const plan = planPreviewSnap(req);
	assert.equal(plan.ok, true, plan.reason);
	assert.ok(Math.abs(plan.rate - 1) < 1e-6, `rate ${plan.rate}`);
});

test('R3 catch-up: the same click planned 0.7 s late is still in phase and on the clicked bar', () => {
	const early = planPreviewSnap(request());
	const late = 0.7;
	const req = request({
		startAtContextSec: 10.05 + late,
		masterPositionAtStartSec: MASTER_AT + late
	});
	const plan = planPreviewSnap(req);
	assert.equal(plan.ok, true, plan.reason);
	const { worst, allSameNumber } = worstOver(req, plan);
	assert.ok(worst < 0.5, `worst ${worst.toFixed(3)} ms`);
	assert.ok(allSameNumber);
	// Still within one bar of the click: catch-up shifts the phase, not the bar.
	assert.ok(Math.abs(plan.previewBeatIndex - early.previewBeatIndex) <= 4);
});

test('R4 a click in the intro snaps to the top of the grid', () => {
	const plan = planPreviewSnap(request({ clickSec: 0.05 }));
	assert.equal(plan.ok, true, plan.reason);
	assert.equal(plan.clamped, true);
	assert.ok(plan.previewBeatIndex <= 4, `index ${plan.previewBeatIndex}`);
});

test('R4 control: a click on the grid is not marked clamped', () => {
	assert.equal(planPreviewSnap(request()).clamped, false);
});

test('BEAT mode takes the nearest beat whatever its number', () => {
	const req = request({ mode: 'beat', clickSec: PREVIEW[160].t + 0.01 });
	const plan = planPreviewSnap(req);
	assert.equal(plan.ok, true, plan.reason);
	assert.equal(plan.previewBeatIndex, 160);
	assert.ok(worstOver(req, plan).worst < 0.5);
});

test('R5 no grid is a refusal, never a throw', () => {
	const plan = planPreviewSnap(request({ previewGrid: [] }));
	assert.equal(plan.ok, false);
});

test('control: the pre-snap preview (start at the click, tempo only) is measurably out', () => {
	const req = request();
	const naive = { startAtContextSec: req.startAtContextSec, offsetSec: req.clickSec, rate: 128 / 124 };
	const m = phaseErrorAt({ gridBeatPosition, ...req, plan: naive, atContextSec: req.startAtContextSec });
	assert.ok(Math.abs(m.errorMs) > 50 || !m.sameBeatNumber, `naive error only ${m.errorMs} ms`);
});

test('R1 late in the master beat: the clicked beat wins, not the one whose landing is nearer', () => {
	// Master 80% through beat 3; the click is a tenth past downbeat 160, so
	// the beat-3s either side are 158 (2.1 beats back) and 162 (1.9 ahead).
	// Snapping by LANDING picks 158, because 158.8 is nearer the click than
	// 162.8. The nearest clicked beat is 162, so that is where it goes.
	const req = request({
		masterPositionAtStartSec: MASTER[202].t + 0.8 * masterBeat,
		clickSec: PREVIEW[160].t + 0.1 * (60 / 124)
	});
	const plan = planPreviewSnap(req);
	assert.equal(plan.ok, true, plan.reason);
	assert.equal(plan.previewBeatIndex, 162);
	assert.ok(worstOver(req, plan).worst < 0.5);
});

// ---------------------------------------------------------------------------
// CUEOUT-6060: the click decision `preview-cue.svelte.ts` obeys.

const NOW = 10;
/** Master read at the start instant (NOW + lead): exactly at MASTER_AT. */
const SNAP_MASTER = { grid: MASTER, positionAtStartSec: MASTER_AT, tempoRatio: 1 };

function startRequest(overrides = {}) {
	return {
		mode: 'phase',
		masterBpm: 128,
		master: SNAP_MASTER,
		previewGrid: PREVIEW,
		trackBpm: 124,
		clickSec: PREVIEW[160].t + 0.05,
		nowContextSec: NOW,
		pitchRangePct: 16,
		...overrides
	};
}

test('R6 phase: the click starts snapped, in phase, one lead after now', () => {
	assert.equal(PREVIEW_SNAP_LEAD_SEC, 0.05, 'the fixture assumes the 50 ms lead');
	const start = previewStartFor(startRequest());
	assert.notEqual(start.snap, null, start.snapRefusal);
	assert.equal(start.snapRefusal, null);
	assert.ok(Math.abs(start.startAtContextSec - (NOW + 0.05)) < 1e-12);
	const req = request({ startAtContextSec: start.startAtContextSec });
	const { worst, allSameNumber } = worstOver(req, start);
	assert.ok(worst < 0.5, `worst ${worst.toFixed(3)} ms`);
	assert.ok(allSameNumber, 'preview left the master beat number');
});

test('R6 phase: the planner uses the master read at the start instant, at its own pitch', () => {
	// The live read projects the master through its schedule (deckMasterAtContextSec),
	// so the planner takes the start-instant position as given, not now plus lead.
	const master = { grid: MASTER, positionAtStartSec: MASTER_AT, tempoRatio: 1.04 };
	const start = previewStartFor(startRequest({ master }));
	assert.notEqual(start.snap, null, start.snapRefusal);
	const req = request({ masterTempoRatio: 1.04, startAtContextSec: start.startAtContextSec });
	assert.ok(worstOver(req, start).worst < 0.5);
});

test('R6 fallback: no preview grid plays from the click at the tempo-match rate', () => {
	const start = previewStartFor(startRequest({ previewGrid: null }));
	assert.equal(start.snap, null);
	assert.match(start.snapRefusal, /beat grid/);
	assert.equal(start.startAtContextSec, 0);
	assert.equal(start.offsetSec, PREVIEW[160].t + 0.05);
	assert.ok(Math.abs(start.rate - 128 / 124) < 1e-12, `rate ${start.rate}`);
});

test('R6 fallback: an unplannable snap (out of range) degrades to the tempo match', () => {
	const start = previewStartFor(
		startRequest({ previewGrid: constantGrid(500, 100, 0.31), trackBpm: 100 })
	);
	assert.equal(start.snap, null);
	assert.match(start.snapRefusal, /\S/);
	assert.equal(start.rate, 1, 'the tempo match refuses 1.28 too');
});

test('R6 fallback: no master names the reason the caller gave', () => {
	const start = previewStartFor(
		startRequest({ master: null, masterBpm: 128, masterRefusal: 'the Rust engine mode decks run on another clock' })
	);
	assert.equal(start.snap, null);
	assert.match(start.snapRefusal, /Rust engine/);
	assert.ok(Math.abs(start.rate - 128 / 124) < 1e-12, 'a known master BPM still tempo-matches');
});

test('R7 tempo: nothing moves, the start is the click and the instant is now', () => {
	const start = previewStartFor(startRequest({ mode: 'tempo' }));
	assert.equal(start.snap, null);
	assert.equal(start.snapRefusal, null);
	assert.equal(start.startAtContextSec, 0);
	assert.equal(start.offsetSec, PREVIEW[160].t + 0.05);
	assert.ok(Math.abs(start.rate - 128 / 124) < 1e-12);
});

test('R7 off: own tempo, from the click', () => {
	const start = previewStartFor(startRequest({ mode: 'off' }));
	assert.deepEqual(
		{ at: start.startAtContextSec, offset: start.offsetSec, rate: start.rate, snap: start.snap, why: start.snapRefusal },
		{ at: 0, offset: PREVIEW[160].t + 0.05, rate: 1, snap: null, why: null }
	);
});

test('R6 phase: a half-tempo lock reports its fold, so the bar counts are not read as matched', () => {
	// A 64 BPM preview against a 128 BPM master locks at double tempo: the start is
	// in phase, but the bar counts differ. The fold must reach the result.
	const half = constantGrid(300, 64, 0.2);
	const start = previewStartFor(startRequest({ previewGrid: half, trackBpm: 64, clickSec: half[80].t + 0.05 }));
	assert.notEqual(start.snap, null, start.snapRefusal);
	assert.notEqual(start.snap.tempoNormalization, 1, 'a 64 against 128 lock is a fold, not a one to one bar match');
});
