// requirement: WAVEZOOM-6802, WAVEZOOM-6802b, WAVEZOOM-6802c, WAVEZOOM-6802d
// [if] the default zoom with no BPM shows anything but the old 24 s window [then] this fails, [else stop].
// [if] a bar is not measured at the reference deck's playing tempo [then] this fails, [else stop].
// [if] "whole track" does not fit the master track across the row [then] this fails, [else stop].
// [if] the elastic playhead leaves center before half the track, or does not land on the track's own place at whole [then] this fails, [else stop].
// [if] a zoom step does not ease in log space from the old window to the new one [then] this fails, [else stop].
// [if] [ or ] stops zooming, or - and = stop resizing the loop [then] this fails, [else stop].
// [if] set_wave_zoom, wave_zoom_step or set_wave_zoom_options stop reaching the prefs and the snapshot [then] this fails, [else stop].
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://wave-zoom.example.test';

let zoom;
let render;
let scrub;
let routing;
let ipc;

before(async () => {
	zoom = {
		...(await loadTypeScriptModule('src/lib/rb/wave-zoom.ts')),
		...(await loadTypeScriptModule('src/lib/rb/wave-zoom-view.ts'))
	};
	render = await loadTypeScriptModule('src/lib/components/rb/wave/render.ts');
	scrub = await loadTypeScriptModule('src/lib/components/rb/wave/wave-scrub.ts');
	routing = await loadTypeScriptModule('src/lib/rb/performance-shortcut-routing.ts');
	await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts', { viteApiBase: API_BASE });
	ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts', { viteApiBase: API_BASE });
});

const ref = (over = {}) => ({ bpm: 128, pitch: 1, durationMs: 300_000, positionMs: 0, ...over });

test('the ladder runs 4 to whole, defaults to 16 bars and stops at both ends', () => {
	assert.deepEqual([...zoom.WAVE_ZOOM_LADDER], ['4', '8', '16', '32', '64', 'whole']);
	assert.equal(zoom.WAVE_ZOOM_DEFAULT, '16');
	assert.equal(zoom.stepWaveZoom('16', 1), '8');
	assert.equal(zoom.stepWaveZoom('16', -1), '32');
	assert.equal(zoom.stepWaveZoom('4', 1), '4');
	assert.equal(zoom.stepWaveZoom('whole', -1), 'whole');
	assert.equal(zoom.waveZoomLabel('16'), '16 BARS');
	assert.equal(zoom.waveZoomLabel('whole'), 'WHOLE TRACK');
});

test('with no BPM the default 16 bars is exactly the old 24 s window', () => {
	assert.equal(zoom.waveZoomWindowS('16', null), render.WAVE_WINDOW_S);
	assert.equal(zoom.waveZoomWindowS('16', ref({ bpm: null })), render.WAVE_WINDOW_S);
});

test('a bar is four beats at the reference deck playing tempo', () => {
	assert.equal(zoom.waveZoomWindowS('16', ref()), (16 * 240) / 128);
	assert.equal(zoom.waveZoomWindowS('4', ref({ pitch: 1.25 })), (4 * 240) / (128 * 1.25));
});

test('whole track is the reference track length in wall-clock seconds', () => {
	assert.equal(zoom.waveZoomWindowS('whole', ref()), 300);
	assert.equal(zoom.waveZoomWindowS('whole', ref({ pitch: 1.2 })), 250);
});

test('on a short track the ladder never narrows on the way out to whole', () => {
	const short = ref({ bpm: null, durationMs: 60_000 });
	const windows = zoom.WAVE_ZOOM_LADDER.map((z) => zoom.waveZoomWindowS(z, short));
	for (let i = 1; i < windows.length; i++) assert.ok(windows[i] >= windows[i - 1], windows.join(' '));
	assert.equal(windows.at(-1), 60);
	assert.equal(windows[0], 6, 'short steps are untouched');
});

test('the reference deck is the master, else the first loaded deck', () => {
	const deck = (id, over) => ({ id, loaded: true, isMaster: false, ...ref(), ...over });
	assert.equal(zoom.waveZoomRef([deck(1, {}), deck(2, { isMaster: true })]).id, 2);
	assert.equal(zoom.waveZoomRef([deck(1, { loaded: false }), deck(3, {})]).id, 3);
	// A loaded master with bad metadata stays the reference; the zoom math then fails loudly.
	const badMaster = deck(1, { isMaster: true, durationMs: null });
	const elected = zoom.waveZoomRef([deck(2, {}), badMaster]);
	assert.equal(elected.id, 1, 'the master is elected by identity, not by its duration');
	assert.throws(() => zoom.waveZoomWindowS('whole', elected), RangeError, 'whole track needs a duration');
	assert.throws(() => zoom.wavePlayheadFrac('elastic', 100, elected), RangeError, 'elastic needs a duration');
	// Control: centre mode never reads the duration, so it still places the playhead at the middle.
	assert.equal(zoom.wavePlayheadFrac('center', 100, elected), 0.5);
});

test('elastic: centered until half the track, then lands on the track own place at whole', () => {
	const at = ref({ positionMs: 250_000 }); // 4:10 of 5:00, the design artifact's example
	assert.equal(zoom.wavePlayheadFrac('elastic', 30, at), 0.5);
	assert.equal(zoom.wavePlayheadFrac('elastic', 150, at), 0.5, 'exactly half the track is still centered');
	const whole = zoom.wavePlayheadFrac('elastic', 300, at);
	assert.ok(Math.abs(whole - 250 / 300) < 1e-9, `whole track puts the playhead at ${whole}`);
	let prev = 0.5;
	for (let w = 150; w <= 300; w += 5) {
		const f = zoom.wavePlayheadFrac('elastic', w, at);
		assert.ok(f >= prev - 1e-12, 'the glide never steps back');
		prev = f;
	}
	assert.equal(zoom.wavePlayheadFrac('center', 300, at), 0.5, 'center mode never moves');
	assert.equal(zoom.wavePlayheadFrac('elastic', 300, null), 0.5, 'no reference deck stays centered');
});

test('a step eases in log space from the old window to the new one, then holds', () => {
	const anim = { from: '16', to: '32', startMs: 1000 };
	const from = zoom.waveZoomWindowS('16', ref());
	const to = zoom.waveZoomWindowS('32', ref());
	assert.ok(Math.abs(zoom.waveZoomAnimatedWindowS(anim, ref(), 1000) - from) < 1e-9);
	const mid = zoom.waveZoomAnimatedWindowS(anim, ref(), 1000 + zoom.WAVE_ZOOM_ANIM_MS / 2);
	assert.ok(mid > from && mid < to);
	assert.equal(zoom.waveZoomAnimatedWindowS(anim, ref(), 1000 + zoom.WAVE_ZOOM_ANIM_MS), to);
	assert.equal(zoom.waveZoomAnimating(anim, 1000 + zoom.WAVE_ZOOM_ANIM_MS), false);
	assert.equal(zoom.waveZoomAnimatedWindowS(anim, ref(), 1001, 0), to, 'reduced motion jumps');
	// Log space: every doubling step takes the same shape, so 64 to whole on a
	// 300 s track and 4 to 8 bars sit at the same ratio partway through.
	const ratio = (a, t) =>
		Math.log(zoom.waveZoomAnimatedWindowS(a, ref(), 1000 + t) / zoom.waveZoomWindowS(a.from, ref())) /
		Math.log(zoom.waveZoomWindowS(a.to, ref()) / zoom.waveZoomWindowS(a.from, ref()));
	const small = ratio({ from: '4', to: '8', startMs: 1000 }, 60);
	const big = ratio({ from: '64', to: 'whole', startMs: 1000 }, 60);
	assert.ok(Math.abs(small - big) < 1e-9);
});

test('the wheel steps once per notch, accumulates trackpad deltas and resets on reversal', () => {
	const acc = { px: 0 };
	assert.equal(zoom.waveZoomWheelSteps(acc, -100), 1, 'wheel up zooms in');
	assert.equal(zoom.waveZoomWheelSteps(acc, 20), 0);
	assert.equal(zoom.waveZoomWheelSteps(acc, 20), 0);
	assert.equal(zoom.waveZoomWheelSteps(acc, 25), -1, 'small deltas add up to a step');
	assert.equal(zoom.waveZoomWheelSteps(acc, 50), 0);
	assert.equal(zoom.waveZoomWheelSteps(acc, -50), 0, 'a reversal starts the count again');
});

test('page-mode and line-mode wheel deltas are converted before they count toward a step', () => {
	// Page mode (2) is one canvas height per unit, so a notch of 1 on a 300 px canvas is 300 px: five steps.
	assert.equal(zoom.waveZoomWheelDeltaPx(1, 2, 300), 300, 'page mode scales by the canvas height');
	assert.equal(zoom.waveZoomWheelSteps({ px: 0 }, zoom.waveZoomWheelDeltaPx(-1, 2, 300)), 5, 'one page-mode notch crosses several thresholds');
	assert.equal(zoom.waveZoomWheelDeltaPx(3, 1, 300), 120, 'line mode is about 40 px a line');
	assert.equal(zoom.waveZoomWheelDeltaPx(-100, 0, 300), -100, 'pixel mode passes through');
});

test('the wheel keeps the delta past a threshold and emits every step a delta crosses', () => {
	// 100 px is one step with 40 px left over; the next 20 px completes the second.
	const acc = { px: 0 };
	assert.equal(zoom.waveZoomWheelSteps(acc, 100), -1);
	assert.equal(zoom.waveZoomWheelSteps(acc, 20), -1, 'the remainder carries into the next event');
	// One 250 px event crosses four thresholds and leaves 10 px.
	const big = { px: 0 };
	assert.equal(zoom.waveZoomWheelSteps(big, 250), -4);
	assert.equal(big.px, 10);
	assert.equal(zoom.waveZoomWheelSteps({ px: 0 }, 0), 0, 'a zero delta is no step and no -0');
	const held = { px: 40 };
	assert.equal(zoom.waveZoomWheelSteps(held, 0), 0);
	assert.equal(zoom.waveZoomWheelSteps(held, 20), -1, 'a zero delta keeps the remainder: 40, 0, 20 crosses 60');
});

test('an interrupted glide starts from the window on screen, not the previous target', () => {
	const shown = zoom.waveZoomWindowS('16', ref());
	const anim = { from: '32', to: '64', startMs: 1000, fromWindowS: shown };
	assert.equal(zoom.waveZoomAnimFromS(anim, ref()), shown);
	assert.ok(Math.abs(zoom.waveZoomAnimatedWindowS(anim, ref(), 1000) - shown) < 1e-9, 'frame zero is on screen');
	assert.equal(zoom.waveZoomAnimFromS({ from: '32', to: '64', startMs: 1000 }, ref()), zoom.waveZoomWindowS('32', ref()));
});

test('every row places the playhead from the reference glide while it animates', () => {
	const r = ref();
	const start = 1000;
	zoom.setWaveZoomGlide(1, { from: '64', to: 'whole', startMs: start }, 220);
	const mid = zoom.waveZoomSharedWindowS(1, 'whole', r, start + 110);
	assert.equal(mid, zoom.waveZoomAnimatedWindowS({ from: '64', to: 'whole', startMs: start }, r, start + 110, 220));
	assert.notEqual(mid, zoom.waveZoomWindowS('whole', r), 'mid-glide is not the final target');
	assert.equal(zoom.waveZoomSharedWindowS(1, 'whole', r, start + 220), zoom.waveZoomWindowS('whole', r));
	assert.equal(zoom.waveZoomSharedWindowS(2, '4', r, start), zoom.waveZoomWindowS('4', r), 'a deck with no glide reads its live window');
	zoom.setWaveZoomGlide(1, { from: '64', to: 'whole', startMs: start }, 0);
	assert.equal(zoom.waveZoomSharedWindowS(1, 'whole', r, start), zoom.waveZoomWindowS('whole', r), 'reduced motion jumps');
});

test('the reference bar length follows the beatgrid tempo at its position', () => {
	const beats = [
		{ t: 0, bpm: 120 },
		{ t: 2, bpm: 120 },
		{ t: 4, bpm: 128 },
		{ t: 6.5, bpm: 128 }
	];
	assert.equal(zoom.beatgridTempoAt(beats, 0), 120);
	assert.equal(zoom.beatgridTempoAt(beats, 3.9), 120, 'before the tempo change');
	assert.equal(zoom.beatgridTempoAt(beats, 4), 128, 'at the change');
	assert.equal(zoom.beatgridTempoAt(beats, 99), 128, 'past the last beat');
	assert.equal(zoom.beatgridTempoAt(beats, -1), 120, 'before the grid starts');
	assert.equal(zoom.beatgridTempoAt([], 1), null);
	for (const bpm of [0, -1, NaN]) {
		assert.throws(() => zoom.beatgridTempoAt([{ t: 0, bpm }], 1), RangeError, `beat bpm ${bpm} is corrupt grid data`);
	}
});

test('the reference bar tempo comes from the beats its row paints', () => {
	const r = { id: 1, loaded: true, isMaster: true, bpm: 120, pitch: 1, durationMs: 300_000, positionMs: 0 };
	const beats = [{ t: 0, bpm: 120 }, { t: 4, bpm: 128 }];
	assert.equal(zoom.waveZoomRefAt(r, 0, null).bpm, 120, 'no published beats: the track BPM');
	assert.equal(zoom.waveZoomRefAt(r, 1000, beats).bpm, 120);
	assert.equal(zoom.waveZoomRefAt(r, 5000, beats).bpm, 128, 'the painted tempo at the position');
	assert.equal(zoom.waveZoomRefAt(r, 5000, null).bpm, 120, 'unpublished again falls back');
});

test('the prefs blob validates zoom keys and names the storage key', () => {
	assert.deepEqual(zoom.validateWaveZoomPrefFields({}, 'k'), {});
	assert.deepEqual(
		zoom.validateWaveZoomPrefFields({ wave_zoom: 'whole', wave_zoom_linked: false, wave_zoom_decks: { 2: '4' } }, 'k'),
		{ wave_zoom: 'whole', wave_zoom_linked: false, wave_zoom_decks: { 2: '4' } }
	);
	assert.throws(() => zoom.validateWaveZoomPrefFields({ wave_zoom: '12' }, 'k'), /^Error: k: malformed/);
	assert.throws(() => zoom.validateWaveZoomPrefFields({ wave_zoom_decks: { 5: '4' } }, 'k'), /unknown deck 5/);
	const prefs = { ...zoom.WAVE_ZOOM_PREF_DEFAULTS, wave_zoom_decks: { 2: '4' } };
	assert.equal(zoom.deckWaveZoom(prefs, 2), '16', 'linked ignores a per-deck step');
	assert.equal(zoom.deckWaveZoom({ ...prefs, wave_zoom_linked: false }, 2), '4');
	assert.equal(zoom.deckWaveZoom({ ...prefs, wave_zoom_linked: false }, 1), '16');
});

function recordingContext() {
	const rects = [];
	return {
		rects,
		fillStyle: '',
		globalAlpha: 1,
		fillRect(x, y, w, h) {
			rects.push({ x, w, alpha: this.globalAlpha });
		}
	};
}

test('a beatgrid varies in tempo only when its beats disagree, so a constant grid costs no per-frame ticks', () => {
	const constant = [{ t: 0, bpm: 120 }, { t: 0.5, bpm: 120 }, { t: 1, bpm: 120.004 }];
	const varying = [{ t: 0, bpm: 120 }, { t: 0.5, bpm: 120 }, { t: 1, bpm: 124 }];
	assert.equal(zoom.beatgridVariesTempo(constant), false, 'a hundredth-of-a-bpm jitter is the same grid');
	assert.equal(zoom.beatgridVariesTempo(varying), true);
	assert.equal(zoom.beatgridVariesTempo(null), false, 'no grid never varies');
	assert.equal(zoom.beatgridVariesTempo([{ t: 0, bpm: 120 }]), false);
});

test('an invalid reference pitch throws instead of silently selecting the 1.5 s bar or a 96 s whole track', () => {
	for (const pitch of [0, -1, NaN]) {
		const ref = { bpm: null, pitch, durationMs: 180_000, positionMs: 0 };
		assert.throws(() => zoom.waveZoomWindowS('16', ref), RangeError, `pitch ${pitch} bars`);
		assert.throws(() => zoom.waveZoomWindowS('whole', ref), RangeError, `pitch ${pitch} whole`);
		assert.throws(() => zoom.wavePlayheadFrac('elastic', 100, ref), RangeError, `pitch ${pitch} playhead`);
	}
	// Control: a genuinely absent BPM still takes the documented fallback.
	assert.equal(zoom.waveZoomWindowS('16', { bpm: null, pitch: 1, durationMs: 180_000, positionMs: 0 }), 16 * 1.5);
});

test('a present but malformed reference BPM throws instead of taking the 1.5 s bar fallback', () => {
	for (const bpm of [0, -120, NaN, Infinity]) {
		const ref = { bpm, pitch: 1, durationMs: 180_000, positionMs: 0 };
		assert.throws(() => zoom.waveZoomWindowS('16', ref), RangeError, `bpm ${bpm}`);
	}
});

test('the click target refuses a playhead place outside the row', () => {
	const base = { centerPositionMs: 60_000, durationMs: 180_000, pointerX: 100, widthPx: 400, windowSeconds: 24 };
	assert.throws(() => scrub.waveClickTargetMs({ ...base, playheadFrac: 1.5 }), RangeError);
	assert.throws(() => scrub.waveClickTargetMs({ ...base, playheadFrac: NaN }), RangeError);
	assert.throws(() => scrub.waveClickTargetMs({ ...base }), RangeError, 'playheadFrac is required');
});

test('the row refuses to paint without its zoom geometry instead of falling back to the 24 s centered view', () => {
	const base = {
		widthCss: 1000, heightCss: 60, positionMs: 0, durationMs: 0, anlz: null,
		palette: { bg: '#000' }, pitch: 1, loop: null
	};
	assert.throws(() => render.drawWaveRow(recordingContext(), { ...base, playheadFrac: 0.5 }), /windowS/, 'windowS is required');
	assert.throws(() => render.drawWaveRow(recordingContext(), { ...base, windowS: 0, playheadFrac: 0.5 }), /windowS/);
	assert.throws(() => render.drawWaveRow(recordingContext(), { ...base, windowS: 24 }), /playheadFrac/, 'playheadFrac is required');
	assert.throws(() => render.drawWaveRow(recordingContext(), { ...base, windowS: 24, playheadFrac: 1.5 }), /playheadFrac/);
	assert.doesNotThrow(() => render.drawWaveRow(recordingContext(), { ...base, windowS: render.WAVE_WINDOW_S, playheadFrac: 0.5 }));
});

test('the row draws its playhead at the zoom place, and a click there lands on the deck position', () => {
	const frame = (playheadFrac) => ({
		widthCss: 1000, heightCss: 60, positionMs: 0, durationMs: 0, anlz: null,
		palette: { bg: '#000' }, pitch: 1, loop: null, windowS: 300, playheadFrac: playheadFrac ?? 0.5
	});
	const core = (ctx) => ctx.rects.find((r) => r.w === 1 && r.alpha === 1);
	const centered = recordingContext();
	render.drawWaveRow(centered, frame(undefined));
	assert.equal(core(centered).x, 500);
	const glided = recordingContext();
	render.drawWaveRow(glided, frame(0.83));
	assert.equal(core(glided).x, 830);
	const target = scrub.waveClickTargetMs({
		centerPositionMs: 250_000, durationMs: 300_000, pointerX: 830, widthPx: 1000,
		windowSeconds: 300, playheadFrac: 0.83
	});
	assert.equal(target, 250_000);
	assert.equal(
		scrub.waveClickTargetMs({ centerPositionMs: 250_000, durationMs: 300_000, pointerX: 500, widthPx: 1000, windowSeconds: 300, playheadFrac: 0.5 }),
		250_000,
		'without a place the click math stays centered'
	);
});

test('[ and ] zoom; - and = still resize the loop', () => {
	const key = (k) => ({ code: '', key: k, target: null, metaKey: false, ctrlKey: false, altKey: false });
	assert.deepEqual(routing.resolvePerformanceShortcutAction(key('[')), { kind: 'wave-zoom', direction: 'out' });
	assert.deepEqual(routing.resolvePerformanceShortcutAction(key(']')), { kind: 'wave-zoom', direction: 'in' });
	assert.deepEqual(routing.resolvePerformanceShortcutAction(key('-')), { kind: 'loop-resize', factor: 0.5 });
	assert.deepEqual(routing.resolvePerformanceShortcutAction(key('=')), { kind: 'loop-resize', factor: 2 });
	assert.equal(routing.resolvePerformanceShortcutAction({ ...key('['), metaKey: true }), null);
});

function installPrefsStorage() {
	const store = new Map();
	const storage = {
		getItem: (k) => (store.has(k) ? store.get(k) : null),
		setItem: (k, v) => store.set(k, v),
		removeItem: (k) => store.delete(k)
	};
	const original = globalThis.localStorage;
	globalThis.localStorage = storage;
	return {
		storage,
		saved: () => JSON.parse(store.get('mdt.rb.ui-prefs.v1') ?? '{}'),
		restore() {
			if (original === undefined) delete globalThis.localStorage;
			else globalThis.localStorage = original;
		}
	};
}

test('the zoom commands reach the prefs, the saved blob and the snapshot', async () => {
	const prefs = installPrefsStorage();
	globalThis.window = { localStorage: prefs.storage };
	const uninstall = ipc.installPerformanceBrowserIpc();
	const dispatch = (c) => window.musicDjToolsPerformance.dispatch(c);
	const ui = () => ipc.queryPerformanceState().ui;
	try {
		assert.equal(ui().wave_zoom, '16');
		assert.equal(ui().wave_zoom_playhead, 'elastic');
		assert.equal(ui().wave_zoom_linked, true);
		await dispatch({ type: 'wave_zoom_step', direction: 'out' });
		assert.equal(ui().wave_zoom, '32');
		await dispatch({ type: 'set_wave_zoom', zoom: 'whole' });
		assert.equal(ui().wave_zoom, 'whole');
		assert.equal(prefs.saved().wave_zoom, 'whole');
		await dispatch({ type: 'set_wave_zoom_options', playhead: 'center', linked: false });
		await dispatch({ type: 'wave_zoom_step', direction: 'in', deck: 2 });
		assert.equal(ui().wave_zoom_decks['2'], '64', 'unlinked, one deck steps on its own');
		assert.equal(ui().wave_zoom_decks['1'], 'whole');
		await assert.rejects(dispatch({ type: 'set_wave_zoom', zoom: '12' }), /wave_zoom must be/);
		await assert.rejects(dispatch({ type: 'wave_zoom_step', direction: 'sideways' }), /direction must be in or out/);
		await dispatch({ type: 'set_wave_zoom_options', playhead: 'elastic', linked: true });
		await dispatch({ type: 'set_wave_zoom', zoom: '16' });
	} finally {
		uninstall();
		delete globalThis.window;
		prefs.restore();
	}
});

test('the playhead core clamps to the last drawable column at the reference end', async () => {
	const { playheadCenterX } = await loadTypeScriptModule('src/lib/components/rb/wave/wave-playhead-render.ts');
	assert.equal(playheadCenterX(1000, 1), 999, 'the exact end stays on the canvas');
	assert.equal(playheadCenterX(1000, 0.5), 500);
	assert.equal(playheadCenterX(1000, 0), 0);
	assert.equal(playheadCenterX(1000, 1.5), 999);
	assert.equal(playheadCenterX(0, 0.5), 0);
});

test('a click on the drawn playhead core seeks to the place it shows, including the exact end', () => {
	// The core paints on column 999 of a 1000 px row at frac 1, so a click there is offset 0.
	const atEnd = scrub.waveClickTargetMs({
		centerPositionMs: 300_000,
		durationMs: 300_000,
		pointerX: 999,
		playheadFrac: 1,
		widthPx: 1000,
		windowSeconds: 100
	});
	assert.equal(atEnd, 300_000, 'the click on the visible core is the reference end, not one pixel before it');
});
