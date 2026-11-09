// requirement: PERF-PAINT-10214
// The master grid ghost walks only the beats in view, and strokes exactly what the whole-list walk did.
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let render;

before(async () => {
	render = await loadTypeScriptModule('src/lib/components/rb/wave/wave-playhead-render.ts');
});

const BEAT_S = 60 / 128;
const W = 1000;
const H = 80;

/** 770 beats at 128 BPM from 0.2 s; reads of `n` count the beats a draw iterates. */
function countingBeats(count = 770) {
	const reads = { n: 0 };
	const beats = Array.from({ length: count }, (_, index) => {
		const beat = { t: 0.2 + index * BEAT_S };
		const n = (index % 4) + 1;
		Object.defineProperty(beat, 'n', {
			get() {
				reads.n += 1;
				return n;
			},
			enumerable: true
		});
		return beat;
	});
	return { beats, reads };
}

function overlayFor(beats, masterPositionSec = 180, followerPositionSec = 60) {
	return {
		masterBeats: beats,
		masterPositionSec,
		followerPositionSec,
		masterPitch: 1,
		followerPitch: 1.02
	};
}

function recordingCanvas() {
	const ops = [];
	return {
		ops,
		save() {},
		restore() {},
		beginPath() {},
		stroke() {
			ops.push(['stroke', this.strokeStyle]);
		},
		moveTo(x, y) {
			ops.push(['moveTo', x, y]);
		},
		lineTo(x, y) {
			ops.push(['lineTo', x, y]);
		},
		strokeStyle: '',
		lineWidth: 0
	};
}

/** The pre-change ghost: every beat projected, off-screen ones dropped. */
function referenceGhost(ctx, overlay, tLeft, pxPerS, w, h, includeDownbeats) {
	const { masterPositionSec, followerPositionSec, masterPitch, followerPitch } = overlay;
	for (const beat of overlay.masterBeats) {
		const downbeat = beat.n === 1;
		if (downbeat && !includeDownbeats) continue;
		if (masterPitch <= 0 || followerPitch <= 0) continue;
		const followerT =
			followerPositionSec + (beat.t - masterPositionSec) * (followerPitch / masterPitch);
		const x = (followerT - tLeft) * pxPerS;
		if (x < -2 || x > w + 2) continue;
		ctx.strokeStyle = downbeat ? 'rgba(255, 255, 255, 0.16)' : 'rgba(255, 255, 255, 0.07)';
		ctx.beginPath();
		ctx.moveTo(x, 0);
		ctx.lineTo(x, h);
		ctx.stroke();
	}
}

test('a 10 s window over a 770-beat overlay iterates at most the beats in view plus 2', () => {
	const { beats, reads } = countingBeats();
	const overlay = overlayFor(beats);
	const pxPerS = W / 10;
	const ctx = recordingCanvas();
	render.drawMasterGridGhost(ctx, overlay, 55, pxPerS, W, H, true);
	const drawn = ctx.ops.filter((op) => op[0] === 'moveTo').length;
	assert.ok(drawn > 0, 'the window has beats in it');
	assert.ok(reads.n <= drawn + 2, `iterated ${reads.n} beats to draw ${drawn}`);
});

for (const [label, masterPositionSec, tLeft] of [
	['start', 2, 0],
	['middle', 180, 55],
	['end', 355, 230]
]) {
	for (const includeDownbeats of [true, false]) {
		test(`the strokes match the whole-list walk at the ${label} (includeDownbeats ${includeDownbeats})`, () => {
			const { beats } = countingBeats();
			const overlay = overlayFor(beats, masterPositionSec, tLeft + 5);
			const pxPerS = W / 10;
			const now = recordingCanvas();
			const before = recordingCanvas();
			render.drawMasterGridGhost(now, overlay, tLeft, pxPerS, W, H, includeDownbeats);
			referenceGhost(before, overlay, tLeft, pxPerS, W, H, includeDownbeats);
			assert.ok(before.ops.length > 0, 'the reference draws something here');
			assert.deepEqual(now.ops, before.ops);
		});
	}
}

test('an empty masterBeats and a window past the last beat draw nothing and do not throw', () => {
	const empty = recordingCanvas();
	render.drawMasterGridGhost(empty, overlayFor([]), 55, W / 10, W, H, true);
	assert.deepEqual(empty.ops, []);
	const { beats, reads } = countingBeats();
	const past = recordingCanvas();
	render.drawMasterGridGhost(past, overlayFor(beats), 10_000, W / 10, W, H, true);
	assert.deepEqual(past.ops, []);
	assert.ok(reads.n <= 2, `a window past the end iterated ${reads.n} beats`);
});

test('the downbeat overlay draws the same downbeats as before over its window', () => {
	const { beats } = countingBeats();
	const overlay = overlayFor(beats);
	const ctx = recordingCanvas();
	render.drawMasterDownbeatOverlay(ctx, overlay, 55, W / 10, W, H);
	const reference = recordingCanvas();
	referenceGhost(reference, overlay, 55, W / 10, W, H, true);
	const downbeatXs = reference.ops.filter(
		(op, i, all) => op[0] === 'moveTo' && all[i + 2]?.[1] === 'rgba(255, 255, 255, 0.16)'
	);
	assert.deepEqual(
		ctx.ops.filter((op) => op[0] === 'moveTo'),
		downbeatXs
	);
});
