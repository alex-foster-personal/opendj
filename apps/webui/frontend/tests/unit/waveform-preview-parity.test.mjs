// requirement: CHROME-18
// [if] the Settings waveform preview paints any design x palette pair with a different set of band colors than the deck row paints for the same sample [then] this fails, [else stop].
// [if] 'blocks' in the mono palette paints one flat color instead of several grays [then] this fails, [else stop].
// [if] the parity check is run against the pre-W15 preview painter (blocks filled with the single mono color) [then] it must report a mismatch, so the guard is shown to bite, [else stop].
// [if] WaveformDesignPreview stops passing the deck row's palette colors, or the painter stops calling the deck's drawWaveBands [then] this fails, [else stop].
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const W = 160;
const H = 36;
const DESIGNS = ['tri-band', 'mono', 'line', 'blocks'];
/** The Settings palette options: three concrete choices plus Auto, which resolves per skin. */
const PALETTE_PREFS = ['rekordbox', 'legacy', 'mono', 'auto'];

let paint;
let sampleMod;
let prePaint;
let render;
let palette;
let skin;

/** Path2D stand-in that keeps the geometry a recording context needs. */
class RecordingPath {
	constructor() {
		this.area = 0;
		this.segments = 0;
	}
	rect(_x, _y, w, h) {
		this.area += Math.max(0, w) * Math.max(0, h);
	}
	moveTo() {}
	lineTo() {
		this.segments += 1;
	}
}

before(async () => {
	globalThis.Path2D = RecordingPath;
	paint = await loadTypeScriptModule('src/lib/settings/waveform-preview-paint.ts');
	sampleMod = await loadTypeScriptModule('src/lib/settings/waveform-preview-sample.ts');
	prePaint = await loadTypeScriptModule('tests/unit/fixtures/waveform-preview-paint-pre-w15.ts');
	render = await loadTypeScriptModule('src/lib/components/rb/wave/render.ts');
	palette = await loadTypeScriptModule('src/lib/rb/wave-palette.ts');
	skin = await loadTypeScriptModule('src/lib/rb/ui-skin.ts');
});

/** A 2D context recording every color that lays down ink (area or stroke
 * segments). Methods the painters may call but that paint nothing are no-ops. */
function recordingContext() {
	const colors = new Set();
	let pathSegments = 0;
	const ink = (color, amount) => {
		if (amount > 0) colors.add(String(color));
	};
	const target = {
		fillStyle: '',
		strokeStyle: '',
		lineWidth: 1,
		globalAlpha: 1,
		clearRect() {},
		fillRect(_x, _y, w, h) {
			ink(target.fillStyle, Math.max(0, w) * Math.max(0, h));
		},
		fill(path) {
			ink(target.fillStyle, path === undefined ? 1 : path.area);
		},
		beginPath() {
			pathSegments = 0;
		},
		moveTo() {},
		lineTo() {
			pathSegments += 1;
		},
		stroke(path) {
			if (target.lineWidth > 0) ink(target.strokeStyle, path === undefined ? pathSegments : path.segments + path.area);
		}
	};
	const ctx = new Proxy(target, {
		get(t, key) {
			if (key in t) return t[key];
			return () => {};
		},
		set(t, key, value) {
			t[key] = value;
			return true;
		}
	});
	return { ctx, colors };
}

/** Band colors the preview painter lays down (its background excluded). */
function previewColors(painter, design, c) {
	const { ctx, colors } = recordingContext();
	painter.paintWaveformPreview(ctx, W, H, design, c, sampleMod.previewSampleWaveform());
	colors.delete(painter.PREVIEW_BACKGROUND);
	return colors;
}

/** The full WavePalette a deck row reads, band entries from `c`, the rest sentinels. */
function deckPalette(c) {
	return {
		bg: '#010203',
		secondaryBg: '#040506',
		low: c.low,
		mid: c.mid,
		high: c.high,
		mono: c.mono,
		tick: '#070809',
		cueHotCue: '#0a0b0c',
		cueLoop: '#0d0e0f',
		cueMemory: '#101112',
		cueOutline: '#131415',
		phrase: '#161718',
		vocal: c.vocal
	};
}

/** Band colors the REAL deck row entry point (drawWaveRow) lays down for the
 * preview's sample: everything a frame with that sample paints, minus what the
 * same frame paints with no analysis (background and playhead). */
function deckColors(design, c) {
	const frame = (anlz) => ({
		windowS: 24,
		playheadFrac: 0.5,
		widthCss: W,
		heightCss: H,
		// The whole 24s window shows the whole sample: tLeft = 0, the row spans it.
		positionMs: (render.WAVE_WINDOW_S * 1000) / 2,
		durationMs: render.WAVE_WINDOW_S * 1000,
		anlz,
		palette: deckPalette(c),
		pitch: 1,
		loop: null,
		waveformDesign: design
	});
	const anlz = {
		stable_id: 'w15-preview-sample',
		points: sampleMod.previewSampleWaveform().detail.length,
		waveform: sampleMod.previewSampleWaveform(),
		beatgrid: { beats: [] },
		cues: [],
		phrases: [],
		vocals: { status: 'not_analyzed' }
	};
	const full = recordingContext();
	render.resetWaveBandCacheForTest();
	render.drawWaveRow(full.ctx, frame(anlz));
	const bare = recordingContext();
	render.drawWaveRow(bare.ctx, frame(null));
	for (const color of bare.colors) full.colors.delete(color);
	return full.colors;
}

function* everyPair() {
	for (const design of DESIGNS) {
		for (const pref of PALETTE_PREFS) {
			// Auto resolves per skin; a concrete choice is the same under every skin.
			const skins = pref === 'auto' ? skin.UI_SKIN_CHOICES : ['default'];
			for (const skinId of skins) {
				const choice = skin.effectiveWavePalette(pref, skinId);
				yield { design, pref, skinId, choice, c: palette.resolveWaveBandColors('dark', choice) };
			}
		}
	}
}

/** Mismatches between a preview painter and the deck row, one line per pair. */
function parityMismatches(painter) {
	const out = [];
	for (const { design, pref, skinId, choice, c } of everyPair()) {
		const preview = [...previewColors(painter, design, c)].sort();
		const deck = [...deckColors(design, c)].sort();
		if (preview.join() !== deck.join()) {
			out.push(`${design} x ${pref}${pref === 'auto' ? `@${skinId}` : ''} (${choice}): preview [${preview}] deck [${deck}]`);
		}
	}
	return out;
}

test('CHROME-18 if the Settings preview uses different band colors than the deck row for any design x palette pair then they have drifted', () => {
	const pairs = [...everyPair()];
	// Control: 4 designs x (3 concrete palettes + Auto under 3 skins). A zero would mean the loop never ran.
	assert.equal(pairs.length, 4 * (3 + 3));
	for (const { design, pref, skinId, choice, c } of pairs) {
		const deck = deckColors(design, c);
		const expected = design === 'tri-band' || design === 'blocks' ? [c.low, c.mid, c.high] : [c.mono];
		// Positive control on the deck side: it paints exactly the palette's band entries for this design.
		assert.deepEqual([...deck].sort(), [...new Set(expected)].sort(), `${design} x ${pref}@${skinId} (${choice}): deck row control`);
	}
	assert.deepEqual(parityMismatches(paint), [], 'preview and deck row paint the same band colors for every pair');
});

test('CHROME-18 if blocks in the mono palette paints one flat color then the Gothic grayscale shading is missing from Settings', () => {
	const c = palette.resolveWaveBandColors('dark', skin.effectiveWavePalette('auto', 'mono-dev'));
	assert.equal(skin.effectiveWaveformDesign('auto', 'mono-dev'), 'blocks', 'precondition: Gothic + Auto is blocks');
	const grays = previewColors(paint, 'blocks', c);
	assert.ok(grays.size >= 2, `blocks x mono must shade in several grays, saw [${[...grays]}]`);
	for (const g of grays) {
		const m = g.match(/^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i);
		assert.ok(m && m[1] === m[2] && m[2] === m[3], `${g} is a gray`);
	}
});

test('CHROME-18 mutation check: the pre-W15 painter (blocks in one color) fails the parity check', () => {
	const mismatches = parityMismatches(prePaint);
	const blocks = mismatches.filter((m) => m.startsWith('blocks x '));
	assert.equal(blocks.length, 6, `every blocks pair must be caught on the pre-fix source, got:\n${mismatches.join('\n')}`);
	assert.ok(
		blocks.some((m) => m.startsWith('blocks x auto@mono-dev')),
		'the reported case (Gothic: blocks + Mono) is among them'
	);
});

test('CHROME-18 if the preview stops painting through the deck renderer or with the deck palette then parity is by luck', () => {
	const painterSrc = readFileSync('src/lib/settings/waveform-preview-paint.ts', 'utf8');
	assert.match(painterSrc, /import \{ drawWaveBands, type BandPalette \} from '\$lib\/components\/rb\/wave\/render';/);
	assert.match(painterSrc, /drawWaveBands\(ctx, sample, w, h, c, design, 0\);/);
	assert.doesNotMatch(painterSrc, /switch \(design\)/, 'no per-design painter of its own');
	const componentSrc = readFileSync('src/lib/components/settings/WaveformDesignPreview.svelte', 'utf8');
	assert.match(componentSrc, /resolveWaveBandColors\('dark', palette\)/);
	assert.doesNotMatch(componentSrc, /resolveStripBandColors/);
});

test('CHROME-18 if the sample chunk has not loaded then the preview paints its background only, never an invented waveform', () => {
	const c = palette.resolveWaveBandColors('dark', 'mono');
	const { ctx, colors } = recordingContext();
	paint.paintWaveformPreview(ctx, W, H, 'blocks', c, null);
	assert.deepEqual([...colors], [paint.PREVIEW_BACKGROUND]);
	// Control: the same call with the sample paints bands.
	assert.ok(previewColors(paint, 'blocks', c).size >= 2);
});

test('CHROME-18 if the real-track sample stops being a lazy chunk then the library first load carries it again', () => {
	const componentSrc = readFileSync('src/lib/components/settings/WaveformDesignPreview.svelte', 'utf8');
	assert.match(componentSrc, /import\('\$lib\/settings\/waveform-preview-sample'\)/);
	for (const file of ['src/lib/components/settings/WaveformDesignPreview.svelte', 'src/lib/settings/waveform-preview-paint.ts']) {
		assert.doesNotMatch(readFileSync(file, 'utf8'), /from '[^']*waveform-preview-sample'/, `${file} imports the sample statically`);
	}
	const s = sampleMod.previewSampleWaveform();
	assert.equal(s.detail.length, sampleMod.PREVIEW_SAMPLE_POINTS);
	for (const band of ['low', 'mid', 'high']) assert.equal(s.detail[band].length, sampleMod.PREVIEW_SAMPLE_POINTS, `${band} band length`);
});
