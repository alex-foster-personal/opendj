/**
 * WAVE-EQ-5230: deck waveforms follow each deck's live EQ and filter, using the
 * band gains the Rust engine reports on its state feed.
 *
 * Regression lines:
 * - if a low gain does not shrink the low band (and only the low band) on the
 *   wavestack row then broken
 * - if unity gains paint differently from no gains then broken (overshoot:
 *   turning the feature on must not change a flat deck)
 * - if an EQ move rebuilds the cached full-track band images then broken: a
 *   knob turn must cost three scaled blits, not a geometry rebuild
 * - if a boosted band can paint outside the waveform area then broken
 * - if a heights-only ('mono' payload) waveform is scaled by band gains then
 *   broken: it has no bands to scale
 * - if the overview strip ignores the gains, or scales a mono payload, then broken
 * - if the setting is off and gains still reach the painter then broken
 */
import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const PALETTE = {
	bg: '#101010',
	low: '#0000aa',
	mid: '#aa8800',
	high: '#ffffff',
	mono: '#88aaff',
	tick: '#ccc',
	cueHotCue: '#35c04f',
	cueLoop: '#e8a13a',
	cueMemory: '#ff3b30',
	cueOutline: '#c8cdd2',
	phrase: '#888'
};
const N = 1200;
const flatBands = (kind = 'tri') => {
	const detail = {
		length: N,
		low: Array(N).fill(0.5),
		mid: Array(N).fill(0.5),
		high: Array(N).fill(0.5)
	};
	return {
		stable_id: 'e'.repeat(40),
		points: N,
		waveform: { kind, preview: detail, detail },
		beatgrid: { beat_count: 0, beats: [] },
		cues: [],
		phrases: [],
		vocals: { status: 'not_analyzed' }
	};
};

class RecordingPath {
	static all = [];
	constructor() {
		this.rects = [];
		this.ys = [];
		RecordingPath.all.push(this);
	}
	rect(x, y, w, h) {
		this.rects.push({ x, y, w, h });
	}
	moveTo(x, y) {
		this.ys.push(y);
	}
	lineTo(x, y) {
		this.ys.push(y);
	}
}

const CENTER_Y = 10 + (60 - 10) / 2; // MARKER_BAND_PX = 10 (wave/cues.ts), 60px row

function recordingContext() {
	const log = { fills: [], images: [], saves: 0, clips: [], restores: 0 };
	const ctx = {
		fillStyle: '',
		strokeStyle: '',
		lineWidth: 1,
		globalAlpha: 1,
		imageSmoothingEnabled: true,
		fillRect(x, y, w, h) {
			log.fills.push({ style: ctx.fillStyle, rects: [{ x, y, w, h }], direct: true });
		},
		fill(path) {
			log.fills.push({ style: ctx.fillStyle, rects: path.rects, direct: false });
		},
		drawImage(...a) {
			const [img] = a;
			if (a.length === 9)
				log.images.push({
					img,
					sx: a[1],
					sw: a[3],
					dx: a[5],
					dy: a[6],
					dw: a[7],
					dh: a[8],
					alpha: ctx.globalAlpha,
					smooth: ctx.imageSmoothingEnabled
				});
			else log.images.push({ img, dx: a[1], dy: a[2], dw: a[3], dh: a[4], alpha: ctx.globalAlpha });
		},
		save() {
			log.saves += 1;
		},
		restore() {
			log.restores += 1;
		},
		rect(x, y, w, h) {
			log.clips.push({ x, y, w, h });
		},
		clip() {},
		beginPath() {},
		moveTo() {},
		lineTo() {},
		closePath() {},
		stroke() {},
		clearRect() {},
		strokeRect() {}
	};
	return { ctx, log };
}

function frame(anlz, extra = {}) {
	return {
		widthCss: 240,
		heightCss: 60,
		positionMs: 30_000,
		durationMs: 120_000,
		anlz,
		palette: PALETTE,
		pitch: 1,
		windowS: 24,
		playheadFrac: 0.5,
		loop: null,
		...extra
	};
}

/** Tallest bar height painted in `style` (node path: geometry is in the fills).
 * `direct` also counts fillRect bars, which the blocks design paints with. */
function tallest(log, style, { direct = false } = {}) {
	const rects = log.fills
		.filter((f) => f.style === style && (direct || !f.direct))
		.flatMap((f) => f.rects);
	assert.ok(rects.length > 0, `control: some ${style} bars were painted`);
	return Math.max(...rects.map((r) => r.h));
}

async function loadRender() {
	return loadTypeScriptModule('src/lib/components/rb/wave/render.ts');
}

async function bandHeightScaleOf() {
	return (await loadTypeScriptModule('src/lib/components/rb/wave/wave-amp.ts')).bandHeightScale;
}

afterEach(() => {
	delete globalThis.document;
});

test('a low gain shrinks only the low band, linearly, on the wavestack row', async () => {
	globalThis.Path2D = RecordingPath;
	const render = await loadRender();
	const flat = recordingContext();
	render.drawWaveRow(flat.ctx, frame(flatBands(), { bandGains: { low: 1, mid: 1, high: 1 } }));
	const cut = recordingContext();
	render.drawWaveRow(cut.ctx, frame(flatBands(), { bandGains: { low: 0.05, mid: 1, high: 1 } }));
	const ratio = tallest(cut.log, PALETTE.low) / tallest(flat.log, PALETTE.low);
	assert.ok(Math.abs(ratio - 0.05) < 1e-9, `low ratio ${ratio}: a 0.05 gain draws 5% height`);
	assert.equal(tallest(cut.log, PALETTE.mid), tallest(flat.log, PALETTE.mid), 'mids untouched');
	assert.equal(tallest(cut.log, PALETTE.high), tallest(flat.log, PALETTE.high), 'highs untouched');
});

test('unity gains paint the same geometry as no gains (turning the feature on changes nothing flat)', async () => {
	globalThis.Path2D = RecordingPath;
	const render = await loadRender();
	const none = recordingContext();
	render.drawWaveRow(none.ctx, frame(flatBands()));
	const unity = recordingContext();
	render.drawWaveRow(unity.ctx, frame(flatBands(), { bandGains: { low: 1, mid: 1, high: 1 } }));
	for (const style of [PALETTE.low, PALETTE.mid, PALETTE.high]) {
		assert.equal(tallest(unity.log, style), tallest(none.log, style), style);
	}
});

function fakeDocument(counter) {
	return {
		createElement(tag) {
			assert.equal(tag, 'canvas');
			counter.n += 1;
			const { ctx } = recordingContext();
			return { width: 0, height: 0, getContext: () => ctx };
		}
	};
}

test('an EQ move rescales cached band layers at blit time and never rebuilds them', async () => {
	globalThis.Path2D = RecordingPath;
	const counter = { n: 0 };
	globalThis.document = fakeDocument(counter);
	const render = await loadRender();
	render.resetWaveBandCacheForTest();
	const anlz = flatBands();
	const a = recordingContext();
	render.drawWaveRow(a.ctx, frame(anlz, { bandGains: { low: 1, mid: 1, high: 1 } }));
	assert.equal(counter.n, 1, 'unity gains (every knob centred) take the single combined image');
	assert.equal(a.log.images.length, 1, 'one blit, exactly as with no gains');

	const b = recordingContext();
	render.drawWaveRow(b.ctx, frame(anlz, { bandGains: { low: 0.05, mid: 0.5, high: 1 } }));
	assert.equal(counter.n, 4, 'a moved knob builds one cached layer per band, once');
	assert.equal(b.log.images.length, 3, 'three blits per frame');
	assert.ok(
		b.log.images.every((i) => i.sw === 240 && i.dw === 240),
		'only the visible row width is blitted'
	);
	assert.ok(
		b.log.images.every((i) => i.smooth === false),
		'scaled bars blit nearest-neighbour'
	);
	assert.equal(b.ctx.imageSmoothingEnabled, true, 'the row context gets its smoothing back');

	const c = recordingContext();
	render.drawWaveRow(c.ctx, frame(anlz, { bandGains: { low: 0.2, mid: 0.7, high: 1 } }));
	assert.equal(counter.n, 4, 'a gain change must not rebuild any layer');
	const [lo, mi, hi] = b.log.images;
	assert.ok(Math.abs(lo.dh - 3) < 1e-9, `low blit height ${lo.dh}: 60px x 0.05`);
	assert.ok(Math.abs(mi.dh - 30) < 1e-9, `mid blit height ${mi.dh}: 60px x 0.5`);
	assert.equal(hi.dh, 60);
	// Scaled about the row centre line, so the centre stays where it was.
	assert.ok(Math.abs(lo.dy + CENTER_Y * 0.05 - CENTER_Y) < 1e-9, 'centre-anchored');
	assert.equal(lo.alpha, 1);
	assert.equal(mi.alpha, 0.9, 'mids keep the combined image alpha');
	assert.equal(b.log.saves, 0, 'no clip without a boost');
});

test('EQ band layers stay tiled at close zoom on a long track (WAVEZOOM-01)', async () => {
	globalThis.Path2D = RecordingPath;
	const made = [];
	globalThis.document = {
		createElement(tag) {
			assert.equal(tag, 'canvas');
			const { ctx } = recordingContext();
			const canvas = { width: 0, height: 0, getContext: () => ctx };
			made.push(canvas);
			return canvas;
		}
	};
	const render = await loadRender();
	render.resetWaveBandCacheForTest();
	const tile = render.BAND_CACHE_CFG.TILE_PX;
	// 10-minute track, 0.5 s window: about 288,000 px of track, far past one canvas.
	const c = recordingContext();
	const at = { durationMs: 600_000, windowS: 0.5, positionMs: Math.round((tile / 480) * 1000) };
	render.drawWaveRow(
		c.ctx,
		frame(flatBands(), { ...at, bandGains: { low: 0.5, mid: 0.5, high: 1 } })
	);
	assert.ok(made.length > 0, 'control: the layered path built canvases');
	assert.ok(
		made.every((cv) => cv.width <= tile),
		`no band canvas wider than one tile: ${made.map((cv) => cv.width)}`
	);
	assert.equal(made.length, 6, 'the visible span straddles one tile edge: two tiles x three bands');
	assert.equal(c.log.images.length, 6, 'each band blits from both visible tiles');
	const lowDw = c.log.images.filter((_, i) => i % 3 === 0).reduce((a, i) => a + i.dw, 0);
	assert.ok(
		Math.abs(lowDw - 240) < 1e-6,
		`the low band covers the visible row width, got ${lowDw}`
	);
});

test('without gains the single combined image is drawn exactly as before', async () => {
	globalThis.Path2D = RecordingPath;
	const counter = { n: 0 };
	globalThis.document = fakeDocument(counter);
	const render = await loadRender();
	render.resetWaveBandCacheForTest();
	const c = recordingContext();
	render.drawWaveRow(c.ctx, frame(flatBands()));
	assert.equal(counter.n, 1);
	assert.equal(c.log.images.length, 1);
});

test('a boosted band is clipped to the waveform area', async () => {
	globalThis.Path2D = RecordingPath;
	globalThis.document = fakeDocument({ n: 0 });
	const render = await loadRender();
	render.resetWaveBandCacheForTest();
	const c = recordingContext();
	render.drawWaveRow(c.ctx, frame(flatBands(), { bandGains: { low: 2, mid: 1, high: 1 } }));
	assert.equal(c.log.saves, 1);
	assert.equal(c.log.restores, 1);
	assert.ok(c.log.images[0].dh > 60, 'control: the boosted layer really is taller than the row');
	const clip = c.log.clips.at(-1);
	assert.ok(
		clip.y >= 10 && clip.y + clip.h <= 60,
		`clip ${JSON.stringify(clip)} stays inside the waveform area`
	);
});

test('a heights-only mono payload is never scaled by band gains', async () => {
	globalThis.Path2D = RecordingPath;
	const counter = { n: 0 };
	globalThis.document = fakeDocument(counter);
	const render = await loadRender();
	render.resetWaveBandCacheForTest();
	const c = recordingContext();
	render.drawWaveRow(
		c.ctx,
		frame(flatBands('mono'), { bandGains: { low: 0.05, mid: 0.05, high: 0.05 } })
	);
	assert.equal(counter.n, 1, 'one combined image, no band layers');
	assert.equal(c.log.images.length, 1);
	assert.equal(c.log.images[0].dh, undefined, 'blitted unscaled');
});

test('the line design bakes gains into its one image and rebuilds only when they move', async () => {
	globalThis.Path2D = RecordingPath;
	const counter = { n: 0 };
	globalThis.document = fakeDocument(counter);
	const render = await loadRender();
	render.resetWaveBandCacheForTest();
	const anlz = flatBands();
	const g = (low) => frame(anlz, { waveformDesign: 'line', bandGains: { low, mid: 1, high: 1 } });
	render.drawWaveRow(recordingContext().ctx, g(1));
	render.drawWaveRow(recordingContext().ctx, g(1));
	assert.equal(counter.n, 1, 'same gains reuse the image');
	render.drawWaveRow(recordingContext().ctx, g(0.2));
	assert.equal(counter.n, 2, 'a moved gain rebuilds the line image');
	// 60px row: half-height 24px, so the key steps every 1/48 of gain (half a pixel).
	render.drawWaveRow(recordingContext().ctx, g(0.051));
	assert.equal(counter.n, 3);
	render.drawWaveRow(recordingContext().ctx, g(0.054));
	assert.equal(
		counter.n,
		4,
		'a move that crosses a half-pixel step rebuilds (the old 1/100 key served 0.051 and 0.054 one image)'
	);
	render.drawWaveRow(recordingContext().ctx, g(0.0545));
	assert.equal(counter.n, 4, 'control: a sub-half-pixel move reuses the image');
});

test('the line design scales its drawn height linearly by the gains', async () => {
	globalThis.Path2D = RecordingPath;
	globalThis.document = fakeDocument({ n: 0 });
	const render = await loadRender();
	const halfExtent = (gains) => {
		render.resetWaveBandCacheForTest();
		RecordingPath.all = [];
		render.drawWaveRow(
			recordingContext().ctx,
			frame(flatBands(), { waveformDesign: 'line', bandGains: gains })
		);
		const ys = RecordingPath.all.flatMap((p) => p.ys);
		assert.ok(ys.length > 0, 'control: the line path was drawn');
		return Math.max(...ys.map((y) => Math.abs(y - CENTER_Y)));
	};
	const ratio = halfExtent({ low: 0.1, mid: 0.1, high: 0.1 }) / halfExtent(null);
	assert.ok(Math.abs(ratio - 0.1) < 1e-9, `line ratio ${ratio}`);
	const oneBand = halfExtent({ low: 0.1, mid: 1, high: 1 }) / halfExtent(null);
	assert.ok(Math.abs(oneBand - 1) < 1e-9, 'control: one cut band leaves the max of the other two');
});

test('the wavestack row and the overview strip shrink a cut band by the same factor', async () => {
	globalThis.Path2D = RecordingPath;
	const render = await loadRender();
	const strip = await loadTypeScriptModule('src/lib/components/rb/deck/strip-waveform-render.ts');
	const gains = { low: 0.1, mid: 1, high: 1 };
	const row = (g) => {
		const c = recordingContext();
		render.drawWaveRow(c.ctx, frame(flatBands(), { bandGains: g }));
		return tallest(c.log, PALETTE.low);
	};
	const stripLow = (g) => {
		const { ctx, log } = recordingContext();
		const bands = {
			length: 4,
			low: [0.8, 0.8, 0.8, 0.8],
			mid: [0.6, 0.6, 0.6, 0.6],
			high: [0.4, 0.4, 0.4, 0.4]
		};
		strip.drawStripPreviewBands(
			ctx,
			bands,
			'tri',
			40,
			40,
			'tri-band',
			{ low: '#0000aa', mid: '#aa8800', high: '#ffffff', mono: '#88aaff' },
			g
		);
		return log.fills.filter((f) => f.style === '#0000aa')[0].rects[0].h;
	};
	const rowRatio = row(gains) / row({ low: 1, mid: 1, high: 1 });
	const stripRatio = stripLow(gains) / stripLow(null);
	assert.ok(Math.abs(rowRatio - stripRatio) < 1e-9, `row ${rowRatio} vs strip ${stripRatio}`);
});

test('bandHeightScale refuses a gain the engine could never report', async () => {
	const bandHeightScale = await bandHeightScaleOf();
	assert.equal(bandHeightScale(1), 1);
	assert.equal(bandHeightScale(0), 0);
	assert.throws(() => bandHeightScale(-0.1), /finite and >= 0/);
	assert.throws(() => bandHeightScale(Number.NaN), /finite and >= 0/);
});

test('the overview strip multiplies each band bar by its gain, and leaves a mono payload alone', async () => {
	const strip = await loadTypeScriptModule('src/lib/components/rb/deck/strip-waveform-render.ts');
	const colors = { low: '#0000aa', mid: '#aa8800', high: '#ffffff', mono: '#88aaff' };
	const bands = {
		length: 4,
		low: [0.8, 0.8, 0.8, 0.8],
		mid: [0.6, 0.6, 0.6, 0.6],
		high: [0.4, 0.4, 0.4, 0.4]
	};
	const heights = (gains, kind = 'tri') => {
		const { ctx, log } = recordingContext();
		strip.drawStripPreviewBands(ctx, bands, kind, 40, 40, 'tri-band', colors, gains);
		const by = (c) => log.fills.filter((f) => f.style === c).map((f) => f.rects[0].h);
		return {
			low: by(colors.low)[0],
			mid: by(colors.mid)[0],
			high: by(colors.high)[0],
			mono: by(colors.mono)[0]
		};
	};
	const flat = heights(null);
	assert.equal(flat.low, 0.8 * 40, 'control: no gains paints the raw band');
	const cut = heights({ low: 0.1, mid: 1, high: 0.5 });
	assert.ok(Math.abs(cut.low - 0.08 * 40) < 1e-9, `low ${cut.low}`);
	assert.equal(cut.mid, flat.mid);
	assert.ok(Math.abs(cut.high - 0.2 * 40) < 1e-9, `high ${cut.high}`);
	const mono = heights({ low: 0.1, mid: 0.1, high: 0.1 }, 'mono');
	assert.equal(mono.mono, 0.8 * 40, 'a heights-only payload has no band to scale');
});

test('parseWaveGains refuses an incomplete engine report', async () => {
	const g = await loadTypeScriptModule('src/lib/rb/wave-eq-gains.svelte.ts');
	assert.equal(
		g.parseWaveGains({ low: 0.05, mid: 1, high: 1 }).low,
		0.05,
		'control: a full report parses'
	);
	assert.throws(() => g.parseWaveGains({ low: 1, mid: 1 }), /wave_gains.high/);
});
// paintBandGains and the engine lifecycle clears are in rust-mode.test.mjs, whose
// one bundle shares `rustMode` with the gains store.

// ----------------------------------------------------------- the setting

const STORAGE_KEY = 'mdt.rb.ui-prefs.v1';
const API_BASE = 'https://wave-eq-reflect.example.test';

function fakePrefsEnv(raw) {
	const store = new Map();
	if (raw !== undefined) store.set(STORAGE_KEY, raw);
	globalThis.window = {
		localStorage: {
			getItem: (k) => (store.has(k) ? store.get(k) : null),
			setItem: (k, v) => store.set(k, v)
		}
	};
	globalThis.document = { documentElement: { dataset: {}, style: {} } };
	return store;
}

test('wave_eq_reflect defaults on, persists, refuses junk, and is applyable from Settings', async () => {
	try {
		const store = fakePrefsEnv();
		const apply = await loadTypeScriptModule('src/lib/settings/apply.ts', {
			viteApiBase: API_BASE
		});
		assert.ok(apply.ALLOWED_SETTING_KEYS.includes('wave_eq_reflect'));
		assert.equal(apply.readSettingValue('wave_eq_reflect'), true, 'default on');
		apply.applySettingChange('wave_eq_reflect', false);
		assert.equal(apply.readSettingValue('wave_eq_reflect'), false);
		assert.equal(JSON.parse(store.get(STORAGE_KEY)).wave_eq_reflect, false, 'persisted');
		assert.throws(() => apply.applySettingChange('wave_eq_reflect', 'yes'));
		const catalog = await loadTypeScriptModule('src/lib/settings/catalog.ts', {
			viteApiBase: API_BASE
		});
		const def = catalog.SETTINGS_CATALOG.find((d) => d.id === 'wave_eq_reflect');
		assert.ok(def && def.implemented && def.control.kind === 'boolean');
		fakePrefsEnv(JSON.stringify({ hide_broken_links: false, wave_eq_reflect: 'on' }));
		await assert.rejects(
			() => loadTypeScriptModule('src/lib/rb/prefs.svelte.ts', { viteApiBase: API_BASE }),
			/wave_eq_reflect is not a boolean/
		);
	} finally {
		delete globalThis.window;
	}
});

test('an idle deck repaints when its band gains move (pinned in the static-repaint effect)', async () => {
	const { readFileSync } = await import('node:fs');
	const src = readFileSync(
		new URL('../../src/lib/components/rb/wave/WaveRow.svelte', import.meta.url),
		'utf8'
	);
	const staticRepaint = src.slice(src.indexOf('// Static repaint on load/seek'));
	assert.match(
		staticRepaint.slice(0, staticRepaint.indexOf('draw(true);')),
		/void paintBandGains\(deckId, uiPrefs\.wave_eq_reflect\);/
	);
	assert.match(
		src,
		/\t\t\tbandGains,\n[\s\S]*?\t\t\tpartnerGains,\n[\s\S]*?\t\t\] as const;/,
		'own and split-partner gains are repaint inputs'
	);
});

test('the blocks design (the default Gothic look) follows the gains on the wavestack row', async () => {
	globalThis.Path2D = RecordingPath;
	const render = await loadRender();
	const lowLed = () => {
		const a = flatBands();
		a.waveform.detail = {
			length: N,
			low: Array(N).fill(0.8),
			mid: Array(N).fill(0.2),
			high: Array(N).fill(0.2)
		};
		return a;
	};
	const lowBar = (gains) => {
		const c = recordingContext();
		render.drawWaveRow(c.ctx, frame(lowLed(), { waveformDesign: 'blocks', bandGains: gains }));
		return tallest(c.log, PALETTE.low, { direct: true });
	};
	const flat = lowBar(null);
	assert.equal(
		lowBar({ low: 1, mid: 1, high: 1 }),
		flat,
		'control: unity gains paint what no gains paint'
	);
	assert.ok(
		lowBar({ low: 0.5, mid: 1, high: 1 }) < flat / 4,
		'a low cut shrinks the low-led blocks'
	);
	assert.equal(
		lowBar({ low: 1, mid: 0.01, high: 1 }),
		flat,
		'control: a mid cut leaves the low-led block height'
	);
	const boosted = recordingContext();
	render.drawWaveRow(
		boosted.ctx,
		frame(lowLed(), { waveformDesign: 'blocks', bandGains: { low: 3, mid: 3, high: 3 } })
	);
	assert.ok(
		tallest(boosted.log, PALETTE.low, { direct: true }) <= flat,
		'a boost is clipped at full height'
	);
});

test('the overview strip blocks design multiplies each band by its gain', async () => {
	const strip = await loadTypeScriptModule('src/lib/components/rb/deck/strip-waveform-render.ts');
	const colors = { low: '#0000aa', mid: '#aa8800', high: '#ffffff', mono: '#88aaff' };
	const bands = {
		length: 4,
		low: [0.8, 0.8, 0.8, 0.8],
		mid: [0.6, 0.6, 0.6, 0.6],
		high: [0.4, 0.4, 0.4, 0.4]
	};
	const lowH = (gains) => {
		const { ctx, log } = recordingContext();
		strip.drawStripPreviewBands(ctx, bands, 'tri', 40, 40, 'blocks', colors, gains);
		const rects = log.fills.filter((f) => f.style === colors.low).flatMap((f) => f.rects);
		assert.ok(rects.length > 0, 'control: low blocks were painted');
		return rects[0].h;
	};
	assert.equal(lowH(null), Math.round(0.8 * 40));
	assert.equal(lowH({ low: 0.5, mid: 1, high: 1 }), Math.round(0.4 * 40));
});
