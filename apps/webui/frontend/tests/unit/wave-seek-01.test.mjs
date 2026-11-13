/**
 * WAVE-SEEK-01: clicking a deck waveform seeks to the spot under the pointer.
 *
 * - if a click on the zoomed waveform lands more than one pixel's duration from the time under the pointer then broken
 * - if a click with the playhead off-centre (elastic zoom) ignores the playhead column then broken
 * - if a quantised click lands more than half a beat from the click then broken
 * - if a quantised click lands off a real beat then broken
 * - if a click with quantise off snaps at all then broken
 * - if a click on the overview strip is not proportional to the whole track then broken
 * - if a waveform seek reaches the engine without exact, so the deck's bar/2-bar/phrase grid re-snaps it, then broken
 * - if a drag across a 120 to 60 BPM change lets the grabbed point slide off the pointer (WAVEZOOM-9661) then broken
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let waveClickSeekMs;
let overviewSeekMs;
let waveSnapModeFromModifiers;
let waveSeekCommand;
let quantizedSeekDecisionMs;
let waveDragTargetMs;
let waveGestureZoomView;
let zoomView;

const BPM = 128;
const BEAT_S = 60 / BPM;
const FIRST_BEAT_S = 0.2;
// 64 bars of 4/4 from 0.2 s: a real, trusted-looking PQTZ grid.
const GRID = Array.from({ length: 256 }, (_, i) => ({ n: (i % 4) + 1, bpm: BPM, t: FIRST_BEAT_S + i * BEAT_S }));
const DURATION_MS = Math.ceil((FIRST_BEAT_S + 256 * BEAT_S) * 1000) + 5_000;
const NO_MODIFIER = { shiftKey: false, metaKey: false };

before(async () => {
	({ waveClickSeekMs, overviewSeekMs, waveSnapModeFromModifiers, waveSeekCommand, waveDragTargetMs, waveGestureZoomView } =
		await loadTypeScriptModule('src/lib/components/rb/wave/wave-scrub.ts'));
	zoomView = await loadTypeScriptModule('src/lib/rb/wave-zoom-view.ts');
	({ quantizedSeekDecisionMs } = await loadTypeScriptModule('src/lib/player/transport/loops.ts'));
});

function _nearestBeatDistanceMs(ms) {
	return Math.min(...GRID.map((b) => Math.abs(b.t * 1000 - ms)));
}

// ------------------------------------------------------------------ zoomed

test('zoomed click: seeks to the time under the pointer within one pixel (centred playhead)', () => {
	const widthPx = 800;
	const windowSeconds = 16; // 20 ms per pixel
	const pxMs = (windowSeconds * 1000) / widthPx;
	for (const pointerX of [0, 37, 250, 399, 400, 401, 613, 799]) {
		const got = waveClickSeekMs({
			centerPositionMs: 60_000,
			pointerX,
			widthPx,
			durationMs: DURATION_MS,
			windowSeconds,
			playheadFrac: 0.5,
			beats: GRID,
			mode: 'exact'
		});
		const want = 60_000 + (pointerX - widthPx * 0.5) * pxMs;
		assert.ok(Math.abs(got - want) <= pxMs, `x=${pointerX}: got ${got} want ${want} (+-${pxMs})`);
	}
});

test('zoomed click: an off-centre playhead column (elastic zoom) is the anchor, not the row centre', () => {
	const widthPx = 1000;
	const windowSeconds = 10; // 10 ms per pixel
	const got = waveClickSeekMs({
		centerPositionMs: 30_000,
		pointerX: 250,
		widthPx,
		durationMs: DURATION_MS,
		windowSeconds,
		playheadFrac: 0.25,
		beats: GRID,
		mode: 'exact'
	});
	assert.ok(Math.abs(got - 30_000) <= 10, `a click on the playhead column stays put, got ${got}`);
});

// --------------------------------------------------------------- quantised

test('quantised click: lands on a real beat within half a beat of the click, never a bar away', () => {
	const mode = waveSnapModeFromModifiers(NO_MODIFIER, true);
	assert.equal(mode, 'beat', 'quantise on snaps to the nearest beat');
	const widthPx = 800;
	const windowSeconds = 16;
	for (let pointerX = 0; pointerX < widthPx; pointerX += 7) {
		const geometry = { centerPositionMs: 60_000, pointerX, widthPx, durationMs: DURATION_MS, windowSeconds, playheadFrac: 0.5 };
		const raw = waveClickSeekMs({ ...geometry, beats: GRID, mode: 'exact' });
		const got = waveClickSeekMs({ ...geometry, beats: GRID, mode });
		assert.ok(Math.abs(got - raw) <= (BEAT_S * 1000) / 2 + 1e-6, `x=${pointerX}: ${got} is more than half a beat from ${raw}`);
		assert.ok(_nearestBeatDistanceMs(got) < 1e-6, `x=${pointerX}: ${got} is not on a beat`);
	}
});

test('quantised click: one beat past a downbeat seeks to that beat, not back to the bar (old default)', () => {
	// Click 1.1 beats after a downbeat: the old default snapped back 1.1 beats to the bar.
	const downbeatMs = GRID[16].t * 1000;
	const clickMs = downbeatMs + 1.1 * BEAT_S * 1000;
	const got = waveClickSeekMs({
		centerPositionMs: clickMs,
		pointerX: 400,
		widthPx: 800,
		durationMs: DURATION_MS,
		windowSeconds: 16,
		playheadFrac: 0.5,
		beats: GRID,
		mode: waveSnapModeFromModifiers(NO_MODIFIER, true)
	});
	assert.equal(got, GRID[17].t * 1000);
});

test('quantise off: a plain click never snaps; Shift is exact and Cmd+Shift snaps to a beat either way', () => {
	assert.equal(waveSnapModeFromModifiers(NO_MODIFIER, false), 'exact');
	assert.equal(waveSnapModeFromModifiers({ shiftKey: false, metaKey: true }, false), 'exact');
	assert.equal(waveSnapModeFromModifiers({ shiftKey: true, metaKey: false }, true), 'exact');
	assert.equal(waveSnapModeFromModifiers({ shiftKey: true, metaKey: true }, false), 'beat');
	const geometry = { centerPositionMs: 60_000, pointerX: 613, widthPx: 800, durationMs: DURATION_MS, windowSeconds: 16, playheadFrac: 0.5 };
	assert.equal(
		waveClickSeekMs({ ...geometry, beats: GRID, mode: waveSnapModeFromModifiers(NO_MODIFIER, false) }),
		waveClickSeekMs({ ...geometry, beats: null, mode: 'exact' })
	);
});

// ---------------------------------------------------------------- overview

test('overview click: proportional to the whole track, quantised within half a beat', () => {
	for (const ratio of [0, 0.1, 0.25, 0.5, 0.77, 1]) {
		const widthPx = 640;
		const pointerX = ratio * widthPx;
		const raw = overviewSeekMs({ pointerX, widthPx, durationMs: DURATION_MS, beats: GRID, mode: 'exact' });
		assert.ok(Math.abs(raw - ratio * DURATION_MS) <= DURATION_MS / widthPx, `ratio ${ratio}: ${raw}`);
		const snapped = overviewSeekMs({ pointerX, widthPx, durationMs: DURATION_MS, beats: GRID, mode: 'beat' });
		const onGrid = raw / 1000 <= GRID[GRID.length - 1].t + BEAT_S;
		if (onGrid) {
			assert.ok(Math.abs(snapped - raw) <= (BEAT_S * 1000) / 2 + 1e-6, `ratio ${ratio}: ${snapped} vs ${raw}`);
		} else {
			assert.equal(snapped, raw, 'off the grid there is no beat to snap to');
		}
	}
	assert.equal(overviewSeekMs({ pointerX: -20, widthPx: 640, durationMs: DURATION_MS, beats: null, mode: 'exact' }), 0);
	assert.equal(overviewSeekMs({ pointerX: 9_999, widthPx: 640, durationMs: DURATION_MS, beats: null, mode: 'exact' }), DURATION_MS);
});

// ------------------------------------------------- engine re-snap (root cause)

test('waveform seeks reach the engine as exact, so a 2-bar grid cannot re-snap them up to 4 beats away', () => {
	assert.deepEqual(waveSeekCommand(2, 61_234.5), { type: 'seek', deck: 2, position_ms: 61_234.5, exact: true });
	// What happened without exact: the deck's 8-beat quantize grid moved a beat-snapped
	// click (2 beats past a 2-bar line) a whole bar away.
	const clickMs = GRID[34].t * 1000;
	const resnapped = quantizedSeekDecisionMs(GRID, clickMs, 8, false, null).targetMs;
	assert.ok(Math.abs(resnapped - clickMs) >= 2 * BEAT_S * 1000 - 1, `engine re-snap moved ${resnapped - clickMs} ms`);
	assert.equal(quantizedSeekDecisionMs(GRID, clickMs, 8, true, null).targetMs, clickMs, 'exact keeps the click');
});

// ------------------------------------------- drag across a tempo change

// 120 BPM for 32 s, then 60 BPM: the 16-bar window doubles from 32 s to 64 s.
const TEMPO_GRID = [
	...Array.from({ length: 64 }, (_, i) => ({ n: (i % 4) + 1, bpm: 120, t: i * 0.5 })),
	...Array.from({ length: 88 }, (_, i) => ({ n: (i % 4) + 1, bpm: 60, t: 32 + i }))
];
const TEMPO_REF = { id: 1, loaded: true, isMaster: true, bpm: 120, pitch: 1, durationMs: 120_000, positionMs: 0 };

/** The live 16-bar view the row paints at a position on the 120 to 60 BPM grid. */
function _liveView(positionMs) {
	const ref = zoomView.waveZoomRefAt(TEMPO_REF, positionMs, TEMPO_GRID);
	const windowS = zoomView.waveZoomWindowS('16', ref);
	return { windowS, frac: zoomView.wavePlayheadFrac('center', windowS, ref) };
}

/** The track time painted under a pointer x for a painted position and view. */
function _timeUnderPointer(positionMs, pointerX, widthPx, view) {
	return waveClickSeekMs({
		centerPositionMs: positionMs,
		pointerX,
		widthPx,
		durationMs: TEMPO_REF.durationMs,
		windowSeconds: view.windowS * TEMPO_REF.pitch,
		playheadFrac: view.frac,
		beats: null,
		mode: 'exact'
	});
}

test('WAVEZOOM-9661: a drag across a 120 to 60 BPM change keeps the grabbed point under the pointer', () => {
	const widthPx = 800;
	const originMs = 30_000;
	const downX = 700;
	const upX = 100;
	assert.equal(_liveView(originMs).windowS, 32, '16 bars at 120 BPM');
	const heldWindowS = waveGestureZoomView(_liveView(originMs), null).windowS;
	const grabbedMs = _timeUnderPointer(originMs, downX, widthPx, waveGestureZoomView(_liveView(originMs), heldWindowS));
	const releaseMs = waveDragTargetMs({
		originPositionMs: originMs,
		originClientX: downX,
		clientX: upX,
		widthPx,
		durationMs: TEMPO_REF.durationMs,
		windowSeconds: heldWindowS * TEMPO_REF.pitch
	});
	assert.equal(releaseMs, 54_000, 'the drag crossed the 32 s tempo change');
	assert.equal(_liveView(releaseMs).windowS, 64, '16 bars at 60 BPM: the live window doubled');
	const painted = waveGestureZoomView(_liveView(releaseMs), heldWindowS);
	assert.equal(painted.windowS, heldWindowS, 'the painted window is the one held at pointerdown');
	assert.equal(painted.frac, _liveView(releaseMs).frac, 'the playhead fraction stays live');
	const underMs = _timeUnderPointer(releaseMs, upX, widthPx, painted);
	assert.ok(Math.abs(underMs - grabbedMs) <= 1, `grabbed ${grabbedMs} ms, under the pointer at release ${underMs} ms`);
	// Without the hold the painted window doubles and the grabbed point slides away.
	const slid = _timeUnderPointer(releaseMs, upX, widthPx, waveGestureZoomView(_liveView(releaseMs), null));
	assert.ok(Math.abs(slid - grabbedMs) > 1_000, `unheld window keeps ${slid} ms under the pointer`);
});

test('WAVEZOOM-9661: with no held window the live view passes through unchanged', () => {
	const live = { windowS: 24, frac: 0.5, cacheWindowS: 48 };
	assert.equal(waveGestureZoomView(live, null), live);
	assert.deepEqual(waveGestureZoomView(live, 24), { windowS: 24, frac: 0.5 });
	assert.throws(() => waveGestureZoomView(live, 0), RangeError);
});
