// requirement: WAVEQS-11730
// [if] show_memory_cue_markers false still paints hot and loop markers only [then] this fails, [else stop].
// [if] each toggle hides exactly its cue kind [then] this fails, [else stop].
// [if] loop:false hides engaged-loop band [then] this fails, [else stop].
import assert from 'node:assert/strict';
import { before, describe, it, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let render;
let cues;
let strip;
let prefs;

before(async () => {
	globalThis.Path2D = class {
		constructor() {
			this.rects = [];
		}
		rect(x, y, w, h) {
			this.rects.push({ x, y, w, h });
		}
	};
	render = await loadTypeScriptModule('src/lib/components/rb/wave/render.ts');
	cues = await loadTypeScriptModule('src/lib/components/rb/wave/cues.ts');
	strip = await loadTypeScriptModule('src/lib/components/rb/deck/strip-waveform-render.ts');
	prefs = await loadTypeScriptModule('src/lib/rb/cue-marker-prefs.ts');
});

const PALETTE = {
	bg: '#101010',
	secondaryBg: '#202020',
	low: '#e8a13a',
	mid: '#4f8fff',
	high: '#f0f0f0',
	mono: '#cccccc',
	tick: '#cccccc',
	cueHotCue: '#35c04f',
	cueLoop: '#e8a13a',
	cueMemory: '#ff3b30',
	cueOutline: '#c8cdd2',
	phrase: '#888888',
	vocal: '#6699cc',
	planFader: '#fff',
	planEqLow: '#fff',
	planEqMid: '#fff',
	planEqHigh: '#fff',
	planFilter: '#fff',
	planOut: '#fff',
	planIn: '#fff',
	sections: { intro: '#111', outro: '#222', tick: '#ccc' }
};

function recordingCtx() {
	const fills = [];
	const fillRects = [];
	const ctx = {
		fillStyle: '',
		strokeStyle: '',
		lineWidth: 1,
		globalAlpha: 1,
		_path: [],
		beginPath() {
			ctx._path = [];
		},
		moveTo(x, y) {
			ctx._path.push(['move', x, y]);
		},
		lineTo(x, y) {
			ctx._path.push(['line', x, y]);
		},
		closePath() {},
		fill(path) {
			if (path !== undefined) return;
			fills.push({ fillStyle: ctx.fillStyle, path: ctx._path.slice() });
		},
		stroke() {},
		fillRect(x, y, w, h) {
			fillRects.push({ x, y, w, h, fillStyle: ctx.fillStyle });
		},
		strokeRect() {},
		clearRect() {}
	};
	return { ctx, fills, fillRects };
}

function loopFills(fillRects) {
	const fill = `rgba(${render.LOOP_ORANGE_RGB}, ${render.LOOP_FILL_ALPHA})`;
	const edge = `rgba(${render.LOOP_ORANGE_RGB}, ${render.LOOP_EDGE_ALPHA})`;
	return fillRects.filter((c) => c.fillStyle === fill || c.fillStyle === edge);
}

function engagedLoop(inMs, outMs) {
	return { in_ms: inMs, out_ms: outMs, engaged: true, beat_length: null };
}

const WIDTH = 240;
const HEIGHT = 60;
const POSITION_MS = 20_000;

function bands(n) {
	const values = Array.from({ length: n }, (_, i) => (i % 8) / 8);
	return { length: n, low: values, mid: values, high: values };
}

function anlzWithCues(cueList) {
	return {
		stable_id: 'a'.repeat(40),
		points: 64,
		waveform: { kind: 'tri', preview: bands(64), detail: bands(64) },
		beatgrid: {
			source: 'rekordbox',
			beat_count: 120,
			beats: Array.from({ length: 120 }, (_, i) => i * 500),
			bar_phase_untrusted: false
		},
		cues: cueList,
		phrases: [],
		vocals: { status: 'not_analyzed' }
	};
}

const HOT_CUE_A = {
	kind: 'hot_cue',
	slot: 'A',
	in_ms: 10_000,
	out_ms: null,
	is_loop: false,
	active_loop: false,
	beat_loop_size: null,
	color_table_index: null,
	comment: null
};

const LOOP_HOT_CUE_B = {
	kind: 'hot_cue',
	slot: 'B',
	in_ms: 15_000,
	out_ms: 17_000,
	is_loop: true,
	active_loop: false,
	beat_loop_size: 8,
	color_table_index: null,
	comment: null
};

const MEMORY_CUE = {
	kind: 'memory',
	slot: null,
	in_ms: 25_000,
	out_ms: null,
	is_loop: false,
	active_loop: false,
	beat_loop_size: null,
	color_table_index: null,
	comment: null
};

const ALL_CUES = [HOT_CUE_A, MEMORY_CUE, LOOP_HOT_CUE_B];

function paintRow(cueMarkers, loop = null) {
	const rec = recordingCtx();
	render.drawWaveRow(rec.ctx, {
		windowS: 24,
		playheadFrac: 0.5,
		widthCss: WIDTH,
		heightCss: HEIGHT,
		positionMs: POSITION_MS,
		durationMs: 300_000,
		anlz: anlzWithCues(ALL_CUES),
		palette: PALETTE,
		pitch: 1,
		loop,
		...(cueMarkers === undefined ? {} : { cueMarkers })
	});
	return rec;
}

function hasFillColor(fills, color) {
	return fills.some((f) => f.fillStyle === color);
}

function hasLoopBandFill(fillRects, color) {
	return fillRects.some((r) => r.fillStyle === color && r.w > 2);
}

describe('WAVEQS-11730 cue marker visibility on deck rows', () => {
	it('paints hot, memory and loop when all visible or cueMarkers absent', () => {
		for (const markers of [undefined, cues.ALL_CUE_MARKERS_VISIBLE]) {
			const { fills, fillRects } = paintRow(markers);
			assert.ok(hasFillColor(fills, PALETTE.cueHotCue), String(markers));
			assert.ok(hasFillColor(fills, PALETTE.cueMemory), String(markers));
			assert.ok(hasLoopBandFill(fillRects, PALETTE.cueLoop), String(markers));
		}
	});

	it('show_memory_cue_markers false paints hot and loop only', () => {
		const { fills, fillRects } = paintRow({ hot: true, memory: false, loop: true });
		assert.ok(hasFillColor(fills, PALETTE.cueHotCue));
		assert.ok(!hasFillColor(fills, PALETTE.cueMemory));
		assert.ok(hasLoopBandFill(fillRects, PALETTE.cueLoop));
	});

	for (const [kind, color, isRect] of [
		['hot', PALETTE.cueHotCue, false],
		['memory', PALETTE.cueMemory, false],
		['loop', PALETTE.cueLoop, true]
	]) {
		it(`${kind} toggle hides only its kind`, () => {
			const hidden = { hot: true, memory: true, loop: true, [kind]: false };
			const { fills, fillRects } = paintRow(hidden);
			if (isRect) {
				assert.ok(!hasLoopBandFill(fillRects, color));
				for (const other of ['hot', 'memory']) {
					const c = other === 'hot' ? PALETTE.cueHotCue : PALETTE.cueMemory;
					assert.ok(hasFillColor(fills, c));
				}
			} else {
				assert.ok(!hasFillColor(fills, color));
				assert.ok(hasLoopBandFill(fillRects, PALETTE.cueLoop));
				const otherColor = kind === 'hot' ? PALETTE.cueMemory : PALETTE.cueHotCue;
				assert.ok(hasFillColor(fills, otherColor));
			}
		});
	}

	it('loop:false hides engaged-loop band', () => {
		const engaged = engagedLoop(64_000, 80_000);
		const rec = recordingCtx();
		render.drawWaveRow(rec.ctx, {
			windowS: 24,
			playheadFrac: 0.5,
			widthCss: WIDTH,
			heightCss: HEIGHT,
			positionMs: 75_000,
			durationMs: 300_000,
			anlz: anlzWithCues(ALL_CUES),
			palette: PALETTE,
			pitch: 1,
			loop: engaged,
			cueMarkers: { hot: true, memory: true, loop: true }
		});
		assert.ok(loopFills(rec.fillRects).length > 0);
		const offRec = recordingCtx();
		render.drawWaveRow(offRec.ctx, {
			windowS: 24,
			playheadFrac: 0.5,
			widthCss: WIDTH,
			heightCss: HEIGHT,
			positionMs: 75_000,
			durationMs: 300_000,
			anlz: anlzWithCues(ALL_CUES),
			palette: PALETTE,
			pitch: 1,
			loop: engaged,
			cueMarkers: { hot: true, memory: true, loop: false }
		});
		assert.equal(loopFills(offRec.fillRects).length, 0);
	});
});

test('cueMarkerKind classifies hot, memory and loop', () => {
	assert.equal(cues.cueMarkerKind(HOT_CUE_A), 'hot');
	assert.equal(cues.cueMarkerKind(MEMORY_CUE), 'memory');
	assert.equal(cues.cueMarkerKind(LOOP_HOT_CUE_B), 'loop');
	assert.equal(cues.cueMarkerKind({ ...MEMORY_CUE, is_loop: true, out_ms: 26_000 }), 'loop');
});

test('drawStripWaveform skips loop rects when cueMarkers.loop is false', () => {
	const rec = recordingCtx();
	strip.drawStripWaveform(rec.ctx, {
		widthPx: 100,
		heightPx: 40,
		durationMs: 60_000,
		waveform: null,
		vocals: null,
		loop: engagedLoop(10_000, 20_000),
		loopCues: [{ in_ms: 5_000, out_ms: 8_000 }],
		cueMarkers: { hot: true, memory: true, loop: false }
	});
	assert.equal(rec.fillRects.filter((r) => r.fillStyle === strip.LOOP_CUE_COLOR).length, 0);
});

test('cue-marker-prefs defaults, validate, setters and hydrate', () => {
	assert.deepEqual(prefs.CUE_MARKER_PREF_DEFAULTS, {
		show_hot_cue_markers: true,
		show_memory_cue_markers: true,
		show_loop_markers: true
	});
	assert.throws(
		() => prefs.validateCueMarkerPrefFields({ show_hot_cue_markers: 'yes' }, 'k'),
		/show_hot_cue_markers is not a boolean/
	);
	const state = { ...prefs.CUE_MARKER_PREF_DEFAULTS };
	const syncs = [];
	const { setShowHotCueMarkers, setShowMemoryCueMarkers, setShowLoopMarkers } =
		prefs.makeCueMarkerSetters(
			state,
			() => {},
			(patch) => syncs.push(patch)
		);
	setShowHotCueMarkers(false);
	assert.equal(state.show_hot_cue_markers, false);
	assert.deepEqual(syncs.at(-1), { show_hot_cue_markers: false });
	setShowMemoryCueMarkers(false);
	setShowLoopMarkers(false);
	assert.deepEqual(syncs.at(-1), { show_loop_markers: false });
	const hydrated = { ...prefs.CUE_MARKER_PREF_DEFAULTS };
	prefs.hydrateCueMarkerPrefs({ show_hot_cue_markers: false, show_loop_markers: 'x' }, hydrated);
	assert.equal(hydrated.show_hot_cue_markers, false);
	assert.equal(hydrated.show_loop_markers, true);
});

test('WAVEQS-11730 quick-settings marker rows are live settings', async () => {
	const qs = await loadTypeScriptModule('src/lib/settings/waveform-quick-settings.ts');
	const catalog = await loadTypeScriptModule('src/lib/settings/catalog.ts');
	const markers = qs.WAVE_QS_GROUPS.find((g) => g.id === 'markers');
	assert.ok(markers);
	for (const id of ['show_hot_cue_markers', 'show_memory_cue_markers', 'show_loop_markers']) {
		const row = markers.rows.find((r) => r.kind === 'setting' && r.id === id);
		assert.ok(row, id);
		const def = qs.resolveWaveQsSetting(catalog.SETTINGS_CATALOG, id);
		assert.equal(def.control.kind, 'boolean');
		assert.match(qs.waveQsRowTitle(def), /Agents: opendj set_waveform_overlay$/);
	}
	for (const gone of ['hot_cues', 'memory_cues', 'loop_cues']) {
		assert.ok(!markers.rows.some((r) => r.kind === 'inert' && r.id === gone), gone);
	}
});
