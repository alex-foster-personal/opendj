/**
 * Requirements: STRUCT-05.
 *
 * "Show sections": each analyzed section painted as a translucent colored
 * band with its label over the main scrolling waveform (drawWaveRow), the
 * deck overview strip (drawStripWaveform) and the browser preview strip.
 *
 * Regression lines:
 * - if the toggle is OFF and any band paints then existing users see a change
 *   they never opted into (the control for every "ON paints" assertion below)
 * - if the toggle is ON and a visible section paints no band, or an
 *   off-screen one does, then the overlay lies about the structure
 * - if a band paints AFTER the beat grid or a cue then it tints them instead
 *   of sitting behind them
 * - if a PSSI kind/mood maps to the wrong name then a rekordbox "Down"
 *   reads as a drop
 * - if an unknown label is given a DJ family color then a guess renders as fact
 * - if spans are rebuilt per frame then the rAF loop allocates per frame
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, describe, it } from 'node:test';
import { fileURLToPath } from 'node:url';

import { loadTypeScriptModule } from './load-typescript.mjs';

let render;
let sections;
let strip;
let prefsMod;

before(async () => {
	// Counts segments so a stroke/fill of an EMPTY path is distinguishable
	// from one that draws the waveform (the line design strokes a path).
	globalThis.Path2D = class {
		ops = 0;
		rect() {
			this.ops += 1;
		}
		moveTo() {
			this.ops += 1;
		}
		lineTo() {
			this.ops += 1;
		}
	};
	// One bundle for render.ts and the section painter's lazy loader, so the
	// preload below fills the loader instance drawWaveRow reads.
	render = await loadTypeScriptModule('tests/unit/fixtures/wave-render-sections-entry.ts');
	await render.loadSectionsApi();
	sections = await loadTypeScriptModule('src/lib/components/rb/wave/sections.ts');
	strip = await loadTypeScriptModule('src/lib/components/rb/deck/strip-waveform-render.ts');
	prefsMod = await loadTypeScriptModule('src/lib/rb/section-overlay-prefs.ts');
});

const SECTION_PALETTE = {
	edge: '#00aaff',
	rise: '#ffaa00',
	drop: '#ff0055',
	calm: '#8877cc',
	body: '#6688aa',
	neutral: '#808080',
	text: '#eeeeee'
};

const PALETTE = {
	bg: '#101010',
	secondaryBg: '#1a1f28',
	low: '#e8a13a',
	mid: '#4f8fff',
	high: '#f0f0f0',
	tick: '#cccccc',
	cueHotCue: '#35c04f',
	cueLoop: '#e8a13a',
	cueMemory: '#ff3b30',
	cueOutline: '#c8cdd2',
	phrase: '#888888',
	sections: SECTION_PALETTE
};

const SECTION_COLORS = new Set(
	Object.entries(SECTION_PALETTE)
		.filter(([k]) => k !== 'text')
		.map(([, v]) => v)
);

/** Ordered log of every paint call with the style and alpha in force. */
function recordingCtx() {
	const sequence = [];
	const fillRects = [];
	const texts = [];
	let measured = 0;
	const ctx = {
		fillStyle: '',
		strokeStyle: '',
		lineWidth: 1,
		globalAlpha: 1,
		font: '',
		textBaseline: 'alphabetic',
		save() {},
		restore() {},
		beginPath() {},
		closePath() {},
		moveTo() {},
		lineTo() {},
		rect() {},
		clip() {
			sequence.push({ kind: 'clip' });
		},
		fill(path) {
			sequence.push({
				kind: path === undefined ? 'fill' : 'fillPath',
				style: ctx.fillStyle,
				ops: path?.ops ?? null
			});
		},
		stroke(path) {
			sequence.push({
				kind: 'stroke',
				style: ctx.strokeStyle,
				alpha: ctx.globalAlpha,
				ops: path?.ops ?? null
			});
		},
		fillRect(x, y, w, h) {
			const r = { kind: 'fillRect', x, y, w, h, style: ctx.fillStyle, alpha: ctx.globalAlpha };
			fillRects.push(r);
			sequence.push(r);
		},
		strokeRect() {
			sequence.push({ kind: 'strokeRect', style: ctx.strokeStyle });
		},
		fillText(text, x, y) {
			const t = { kind: 'fillText', text, x, y, style: ctx.fillStyle, font: ctx.font };
			texts.push(t);
			sequence.push(t);
		},
		measureText(text) {
			measured += 1;
			return { width: text.length * 5 };
		},
		clearRect() {}
	};
	return { ctx, sequence, fillRects, texts, measureCount: () => measured };
}

// WAVE_WINDOW_S is 24s; width 240 gives 10px/s. positionMs 20000 centers the
// window on 8s..32s.
const WIDTH = 240;
const HEIGHT = 60;
const POSITION_MS = 20_000;

const OWN = [
	{ start_s: 0, end_s: 10, kind: 0, mood: 0, label: 'intro', source: 'own' },
	{ start_s: 10, end_s: 20, kind: 0, mood: 0, label: 'drop', source: 'own' },
	{ start_s: 20, end_s: 40, kind: 0, mood: 0, label: 'breakdown', source: 'own' },
	{ start_s: 40, end_s: 60, kind: 0, mood: 0, label: 'outro', source: 'own' }
];

function bands(n) {
	const values = Array.from({ length: n }, (_, i) => (i % 8) / 8);
	return { length: n, low: values, mid: values, high: values };
}

function anlz(phrases, { beats = [], cues = [] } = {}) {
	return {
		stable_id: 'a'.repeat(40),
		points: 64,
		waveform: { kind: 'tri', preview: bands(64), detail: bands(64) },
		beatgrid: { source: 'rekordbox', beat_count: beats.length, beats },
		cues,
		phrases,
		vocals: { status: 'not_analyzed' }
	};
}

function paintRow(showSections, phrases = OWN, extra, waveformDesign) {
	const rec = recordingCtx();
	render.drawWaveRow(rec.ctx, {
		waveformDesign,
		windowS: 24,
		playheadFrac: 0.5,
		widthCss: WIDTH,
		heightCss: HEIGHT,
		positionMs: POSITION_MS,
		durationMs: 300_000,
		anlz: anlz(phrases, extra),
		palette: PALETTE,
		pitch: 1,
		loop: null,
		showSections
	});
	return rec;
}

/** Full-height section bodies: a section color at the body alpha. */
function bodies(rec) {
	return rec.fillRects.filter(
		(r) => SECTION_COLORS.has(r.style) && r.h === HEIGHT && r.w > 1 && r.alpha < 0.3
	);
}

describe('section bands on the main waveform (drawWaveRow)', () => {
	it('CONTROL: toggle off paints no section band and no label', () => {
		const rec = paintRow(false);
		assert.equal(bodies(rec).length, 0);
		assert.equal(
			rec.fillRects.filter((r) => SECTION_COLORS.has(r.style)).length,
			0,
			'no section color at all may be painted with the toggle off'
		);
		assert.equal(rec.texts.length, 0);
	});

	it('CONTROL: an omitted showSections behaves as off (existing callers unchanged)', () => {
		const rec = recordingCtx();
		render.drawWaveRow(rec.ctx, {
			windowS: 24,
			playheadFrac: 0.5,
			widthCss: WIDTH,
			heightCss: HEIGHT,
			positionMs: POSITION_MS,
			durationMs: 300_000,
			anlz: anlz(OWN),
			palette: PALETTE,
			pitch: 1,
			loop: null
		});
		assert.equal(bodies(rec).length, 0);
	});

	for (const design of ['tri-band', 'mono', 'line', 'blocks', 'rgb']) {
		it(`waveform design ${design}: bands paint under a non-empty waveform, and off paints none`, () => {
			const on = paintRow(true, OWN, undefined, design);
			assert.deepEqual(
				bodies(on).map((r) => r.style),
				[SECTION_PALETTE.edge, SECTION_PALETTE.drop, SECTION_PALETTE.calm]
			);
			const waveIdx = on.sequence.findIndex(
				(e) =>
					((e.kind === 'fillPath' || e.kind === 'stroke') && e.ops !== null && e.ops > 0) ||
					// Blocks bars paint with fillRect, not a path (PGWAVE-7692b).
					(design === 'blocks' && e.kind === 'fillRect' && [PALETTE.low, PALETTE.mid, PALETTE.high].includes(e.style))
			);
			assert.ok(waveIdx >= 0, `the ${design} waveform must paint a non-empty path`);
			const lastBody = on.sequence.lastIndexOf(bodies(on).at(-1));
			assert.ok(lastBody < waveIdx, 'section bodies paint before the waveform');
			const off = paintRow(false, OWN, undefined, design);
			assert.equal(bodies(off).length, 0);
			assert.equal(off.texts.length, 0);
		});
	}

	it('sectionLabels false (the flipped split-row half) keeps the bands and drops the labels', () => {
		const rec = paintRow(true, OWN, undefined, 'blocks');
		assert.ok(rec.texts.length > 0, 'CONTROL: the same frame with labels on draws section names');
		const recordNoLabels = recordingCtx();
		render.drawWaveRow(recordNoLabels.ctx, {
			waveformDesign: 'blocks',
			blocksMirrored: false,
			windowS: 24,
			playheadFrac: 0.5,
			widthCss: WIDTH,
			heightCss: HEIGHT,
			positionMs: POSITION_MS,
			durationMs: 300_000,
			anlz: anlz(OWN),
			palette: PALETTE,
			pitch: 1,
			loop: null,
			showSections: true,
			sectionLabels: false
		});
		assert.deepEqual(
			bodies(recordNoLabels).map((r) => r.style),
			[SECTION_PALETTE.edge, SECTION_PALETTE.drop, SECTION_PALETTE.calm]
		);
		assert.equal(recordNoLabels.texts.length, 0);
	});

	it('toggle on paints one band per VISIBLE section, in its family color, clipped to the row', () => {
		const rec = paintRow(true);
		const got = bodies(rec).map((r) => ({ x: r.x, w: r.w, style: r.style }));
		// intro 0..10s -> visible 8..10s -> x 0..20; drop 10..20s -> x 20..120;
		// breakdown 20..40s -> x 120..240 (clipped); outro 40..60s is off-screen.
		assert.deepEqual(got, [
			{ x: 0, w: 20, style: SECTION_PALETTE.edge },
			{ x: 20, w: 100, style: SECTION_PALETTE.drop },
			{ x: 120, w: 120, style: SECTION_PALETTE.calm }
		]);
	});

	it('labels paint in the marker band at the section start and skip too-narrow bands', () => {
		const rec = paintRow(true);
		const labels = rec.texts.map((t) => t.text);
		// The visible 20px of the intro is under SECTION_MIN_LABEL_PX once the
		// chevron pad is taken: no text. Drop and Breakdown have room.
		assert.deepEqual(labels, ['Drop', 'Breakdown']);
		const drop = rec.texts[0];
		assert.ok(drop.x > 20 && drop.x < 30, `label x ${drop.x} not at the section start`);
		assert.ok(drop.y < 10, 'label must sit inside the 10px marker band');
		assert.equal(drop.style, SECTION_PALETTE.text);
	});

	it('a section whose start scrolled off keeps ONE sticky label at the left edge', () => {
		const long = [{ start_s: 0, end_s: 60, kind: 0, mood: 0, label: 'drop', source: 'own' }];
		const rec = paintRow(true, long); // window 8..32s: the start sits at x -80
		assert.deepEqual(
			rec.texts.map((t) => [t.text, t.x]),
			[['Drop', 6]],
			'exactly one label, pinned just inside the left edge'
		);
	});

	it('CONTROL: a visible section start gets its start label and no sticky duplicate', () => {
		const rec = paintRow(true, [{ start_s: 10, end_s: 60, kind: 0, mood: 0, label: 'drop', source: 'own' }]);
		assert.deepEqual(rec.texts.map((t) => [t.text, t.x]), [['Drop', 26]]);
		// Start just off the left edge: pinned to the edge, never drawn twice.
		const edge = paintRow(true, [{ start_s: 7, end_s: 60, kind: 0, mood: 0, label: 'drop', source: 'own' }]);
		assert.deepEqual(edge.texts.map((t) => [t.text, t.x]), [['Drop', 6]]);
	});

	it('bodies paint before the waveform; bodies and labels before the beat grid and cues', () => {
		const beats = [
			{ n: 1, bpm: 120, t: 16 },
			{ n: 2, bpm: 120, t: 16.5 }
		];
		const cue = {
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
		const { sequence } = paintRow(true, OWN, { beats, cues: [cue] });
		const lastBody = sequence.findLastIndex((c) => c.kind === 'fillRect' && SECTION_COLORS.has(c.style));
		const lastLabel = sequence.findLastIndex((c) => c.kind === 'fillText');
		const firstWave = sequence.findIndex((c) => c.kind === 'fillPath');
		const firstTick = sequence.findIndex((c) => c.kind === 'fillRect' && c.style === PALETTE.tick);
		const firstCue = sequence.findIndex((c) => c.kind === 'fill' && c.style === PALETTE.cueMemory);
		assert.ok(lastBody >= 0 && lastLabel >= 0 && firstWave >= 0 && firstTick >= 0 && firstCue >= 0);
		assert.ok(lastBody < firstWave, 'section bodies must sit behind the waveform');
		assert.ok(Math.max(lastBody, lastLabel) < firstTick, 'sections must sit behind the beat grid');
		assert.ok(Math.max(lastBody, lastLabel) < firstCue, 'sections must sit behind cue markers');
	});

	it('bodies stay low alpha so the waveform is never obscured', () => {
		for (const r of bodies(paintRow(true))) {
			assert.ok(r.alpha > 0 && r.alpha <= 0.2, `body alpha ${r.alpha}`);
		}
	});

	it('ambient intro/outro paint lighter and hatched', () => {
		const phrases = [
			{ start_s: 0, end_s: 20, kind: 0, mood: 0, label: 'intro', source: 'own', ambient: true },
			{ start_s: 20, end_s: 40, kind: 0, mood: 0, label: 'intro', source: 'own' }
		];
		const rec = paintRow(true, phrases);
		const [ambient, plain] = bodies(rec);
		assert.ok(ambient.alpha < plain.alpha, 'ambient band must paint lighter than a plain one');
		const hatch = rec.sequence.filter(
			(c) => c.kind === 'stroke' && c.style === SECTION_PALETTE.edge
		);
		assert.equal(hatch.length, 1, 'exactly the ambient band is hatched');
	});

	it('spans are memoized per phrases array and labels measured once, not per frame', () => {
		const phrases = OWN.map((p) => ({ ...p }));
		const a = sections.sectionSpans(phrases);
		assert.equal(sections.sectionSpans(phrases), a, 'same array identity must return the same spans');
		const rec = recordingCtx();
		for (let frame = 0; frame < 5; frame++) {
			sections.drawSectionLabels(rec.ctx, a, 8, 10, WIDTH, SECTION_PALETTE, 10);
		}
		// Drop and Breakdown are labeled; the 20px intro sliver is skipped
		// before measuring. Five frames, two measurements.
		assert.equal(rec.measureCount(), 2, 'measureText must run once per labeled span, not per frame');
		assert.equal(rec.texts.length, 10, 'two labels per frame for five frames');
		// A new payload (new array) is a fresh build.
		assert.notEqual(sections.sectionSpans(OWN.map((p) => ({ ...p }))), a);
	});

	it('the painter visits only visible spans (binary search, early break)', () => {
		const many = Array.from({ length: 200 }, (_, i) => ({
			start_s: i * 10,
			end_s: i * 10 + 10,
			kind: 0,
			mood: 0,
			label: 'drop',
			source: 'own'
		}));
		const spans = sections.sectionSpans(many);
		let reads = 0;
		const proxied = new Proxy(spans, {
			get(target, prop, recv) {
				if (typeof prop === 'string' && /^\d+$/.test(prop)) reads += 1;
				return Reflect.get(target, prop, recv);
			}
		});
		const rec = recordingCtx();
		// Window 1000s..1024s: spans 100, 101, 102 are visible.
		sections.drawSectionBands(rec.ctx, proxied, 1000, 10, WIDTH, HEIGHT, SECTION_PALETTE, 0);
		assert.equal(bodies(rec).length, 3);
		assert.ok(reads < 20, `painter read ${reads} spans for 3 visible of 200`);
	});
});

describe('PSSI kind/mood names and label families', () => {
	it('maps every documented high-mood id (crate-digger mood_high_phrase)', () => {
		const high = sections.PSSI_MOOD_HIGH;
		assert.equal(sections.pssiPhraseName(high, 1), 'Intro');
		assert.equal(sections.pssiPhraseName(high, 2), 'Up');
		assert.equal(sections.pssiPhraseName(high, 3), 'Down');
		assert.equal(sections.pssiPhraseName(high, 4), null, 'high mood has no id 4');
		assert.equal(sections.pssiPhraseName(high, 5), 'Chorus');
		assert.equal(sections.pssiPhraseName(high, 6), 'Outro');
		assert.equal(sections.pssiPhraseName(high, 7), null);
	});

	it('maps mid mood ids 1..10 (Intro, Verse 1-6, Bridge, Chorus, Outro)', () => {
		const mid = sections.PSSI_MOOD_MID;
		const want = [
			'Intro', 'Verse 1', 'Verse 2', 'Verse 3', 'Verse 4', 'Verse 5', 'Verse 6',
			'Bridge', 'Chorus', 'Outro'
		];
		want.forEach((name, i) => assert.equal(sections.pssiPhraseName(mid, i + 1), name));
		assert.equal(sections.pssiPhraseName(mid, 11), null);
	});

	it('maps low mood ids including the three-id Verse 1 / Verse 2 groups', () => {
		const low = sections.PSSI_MOOD_LOW;
		const want = [
			'Intro', 'Verse 1', 'Verse 1', 'Verse 1', 'Verse 2', 'Verse 2', 'Verse 2',
			'Bridge', 'Chorus', 'Outro'
		];
		want.forEach((name, i) => assert.equal(sections.pssiPhraseName(low, i + 1), name));
	});

	it('an unknown mood names nothing rather than guessing', () => {
		assert.equal(sections.pssiPhraseName(0, 1), null);
		assert.equal(sections.pssiPhraseName(4, 1), null);
	});

	it('rekordbox phrases get names and DJ families; unknown kinds fall back neutral', () => {
		const spans = sections.sectionSpans([
			{ start_s: 0, end_s: 8, kind: 1, mood: 1 },
			{ start_s: 8, end_s: 16, kind: 2, mood: 1 },
			{ start_s: 16, end_s: 24, kind: 5, mood: 1 },
			{ start_s: 24, end_s: 32, kind: 3, mood: 1 },
			{ start_s: 32, end_s: 40, kind: 4, mood: 2 },
			{ start_s: 40, end_s: 48, kind: 4, mood: 1 }
		]);
		assert.deepEqual(
			spans.map((s) => [s.label, s.family]),
			[
				['Intro', 'edge'],
				['Up', 'rise'],
				['Chorus', 'drop'],
				['Down', 'calm'],
				['Verse 3', 'body'],
				['Phrase 4', 'neutral']
			]
		);
	});

	it('own DJ labels and pop words get their family; an unknown label falls back neutral', () => {
		const fam = sections.sectionFamilyForLabel;
		assert.equal(fam('intro'), 'edge');
		assert.equal(fam('outro'), 'edge');
		assert.equal(fam('buildup'), 'rise');
		assert.equal(fam('drop'), 'drop');
		assert.equal(fam('breakdown'), 'calm');
		assert.equal(fam('cooldown'), 'calm');
		assert.equal(fam('chorus'), 'drop');
		assert.equal(fam('verse'), 'body');
		assert.equal(fam('inst'), 'body');
		assert.equal(fam('Verse 2'), 'body');
		assert.equal(fam('mystery'), 'neutral');
		assert.equal(fam(''), 'neutral');
	});

	it('an unknown own label paints in the neutral color with its raw text', () => {
		const rec = paintRow(true, [
			{ start_s: 10, end_s: 30, kind: 0, mood: 0, label: 'mystery', source: 'own' }
		]);
		const [band] = bodies(rec);
		assert.equal(band.style, SECTION_PALETTE.neutral);
		assert.deepEqual(rec.texts.map((t) => t.text), ['Mystery']);
	});
});

describe('section bands on the deck overview strip', () => {
	function paintStrip(sectionsArg) {
		const rec = recordingCtx();
		strip.drawStripWaveform(rec.ctx, {
			widthPx: 400,
			heightPx: 40,
			durationMs: 60_000,
			waveform: { kind: 'tri', preview: bands(16) },
			vocals: null,
			loop: null,
			loopCues: [],
			sections: sectionsArg
		});
		return rec;
	}

	it('CONTROL: null sections paints no band', () => {
		const rec = paintStrip(null);
		assert.equal(rec.fillRects.filter((r) => SECTION_COLORS.has(r.style)).length, 0);
	});

	it('paints one full-height band per section, behind the preview bars, no canvas text', () => {
		const rec = paintStrip({ spans: sections.sectionSpans(OWN), palette: SECTION_PALETTE, drawBands: sections.drawSectionBands });
		const got = rec.fillRects
			.filter((r) => SECTION_COLORS.has(r.style) && r.h === 40 && r.w > 1 && r.alpha < 0.3)
			.map((r) => [Math.round(r.x), Math.round(r.w), r.style]);
		// 60s over 400px: 0..10s -> 0..67, 10..20 -> 67..133, 20..40 -> 133..267, 40..60 -> 267..400.
		assert.deepEqual(got, [
			[0, 67, SECTION_PALETTE.edge],
			[67, 67, SECTION_PALETTE.drop],
			[133, 133, SECTION_PALETTE.calm],
			[267, 133, SECTION_PALETTE.edge]
		]);
		assert.equal(rec.texts.length, 0, 'the stretched strip canvas must not draw text');
		const lastSection = rec.sequence.findLastIndex(
			(c) => c.kind === 'fillRect' && SECTION_COLORS.has(c.style)
		);
		// Any non-section fill is a preview bar: the strip's band colors follow
		// the active wave palette (issue #4219), so no single hex is pinned here.
		const firstBar = rec.sequence.findIndex((c) => c.kind === 'fillRect' && !SECTION_COLORS.has(c.style));
		assert.ok(firstBar >= 0 && lastSection < firstBar, 'bands must sit behind the preview bars');
	});
});

describe('show_sections pref', () => {
	it('defaults OFF', () => {
		assert.equal(prefsMod.SECTION_OVERLAY_PREF_DEFAULTS.show_sections, false);
	});

	it('the setter writes state, persists and PUTs the same key', () => {
		const state = { show_sections: false };
		let persisted = 0;
		const patches = [];
		const s = prefsMod.makeSectionOverlaySetters(state, () => (persisted += 1), (p) => patches.push(p));
		s.setShowSections(true);
		s.toggleShowSections();
		assert.equal(state.show_sections, false);
		assert.equal(persisted, 2);
		assert.deepEqual(patches, [{ show_sections: true }, { show_sections: false }]);
	});

	it('an absent key keeps the default; a wrong-typed key throws naming the storage key', () => {
		assert.deepEqual(prefsMod.validateSectionOverlayPrefFields({}, 'k'), {});
		assert.deepEqual(prefsMod.validateSectionOverlayPrefFields({ show_sections: true }, 'k'), {
			show_sections: true
		});
		assert.throws(
			() => prefsMod.validateSectionOverlayPrefFields({ show_sections: 'yes' }, 'mdt.test'),
			/mdt\.test.*show_sections/
		);
	});

	it('every waveform surface reads the pref, and the Phrase row carries the toggle', () => {
		const read = (rel) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8');
		const root = '../../src/lib/components/rb/';
		assert.match(read(`${root}wave/WaveRow.svelte`), /showSections: uiPrefs\.show_sections/);
		assert.match(read(`${root}wave/WaveRow.svelte`), /void sectionsApiFor\(uiPrefs\.show_sections\)/);
		assert.match(read(`${root}deck/StripWaveform.svelte`), /sectionsApiFor\(uiPrefs\.show_sections && deck\.anlz !== null\)/);
		assert.match(read(`${root}browser/PreviewStrip.svelte`), /_draw\([^;]*sectionsApiFor\(uiPrefs\.show_sections\)\)/);
		assert.match(read(`${root}browser/PreviewStrip.svelte`), /if \(sectionsApi !== null && markerAnlz !== null/);
		const menu = read(`${root}AnalysisSourceMenu.svelte`);
		assert.match(menu, /feature === 'phrase'/);
		assert.match(menu, /setShowSections\(/);
	});
});
