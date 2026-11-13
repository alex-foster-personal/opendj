/** Regenerate the gothic pre-B5 golden (from the pinned pre-B5 painter) and the Settings preview sample from the deck-window fixtures. */
import assert from 'node:assert/strict';
import { readFileSync, writeFileSync } from 'node:fs';
import { captureGothicGolden } from '../tests/unit/fixtures/capture-gothic-golden.mjs';

const FIX = new URL('../tests/unit/fixtures/', import.meta.url);

const PREVIEW_START_S = 8;
const PREVIEW_END_S = 20;
const PREVIEW_SECONDS = PREVIEW_END_S - PREVIEW_START_S;

function encodeBand(values) {
	const raw = Buffer.alloc(values.length);
	for (let i = 0; i < values.length; i++) {
		raw[i] = Math.max(0, Math.min(127, Math.round(values[i] * 127)));
	}
	return raw.toString('base64');
}

function downsampleMaxPairs(arr) {
	const out = [];
	for (let i = 0; i < arr.length; i += 2) {
		const a = arr[i];
		const b = i + 1 < arr.length ? arr[i + 1] : a;
		out.push(Math.max(a, b));
	}
	return out;
}

function slicePreviewBands(detail, windowSpanS, n) {
	const i0 = Math.floor((PREVIEW_START_S / windowSpanS) * n);
	const i1 = Math.floor((PREVIEW_END_S / windowSpanS) * n);
	const slice = (band) => downsampleMaxPairs(band.slice(i0, i1));
	return {
		low: slice(detail.low),
		mid: slice(detail.mid),
		high: slice(detail.high),
		length: Math.ceil((i1 - i0) / 2)
	};
}

async function writePreviewSample() {
	const fx = JSON.parse(readFileSync(new URL('blocks-variance-anlz-window.json', FIX), 'utf8'));
	const windowSpanS = fx.window_s[1] - fx.window_s[0];
	const n = fx.detail.low.length;
	const bands = slicePreviewBands(fx.detail, windowSpanS, n);
	assert.equal(bands.length, bands.low.length);
	const bpm = fx.beats[0]?.bpm ?? 128;
	const t0 = fx.window_s[0];
	const beats = fx.beats
		.filter((b) => b.t >= t0 + PREVIEW_START_S && b.t < t0 + PREVIEW_END_S)
		.map((b) => ({ t: b.t - (t0 + PREVIEW_START_S), n: b.n, bpm: b.bpm ?? bpm }));

	const samplePath = new URL('../src/lib/settings/waveform-preview-sample.ts', import.meta.url);
	const beatsPath = new URL('../src/lib/settings/waveform-preview-beats.ts', import.meta.url);
	const points = bands.low.length;

	const sampleSrc = `/**
 * The Settings waveform preview's sample: a synthetic tri-band detail slice from
 * tests/unit/fixtures/blocks-variance-anlz-window.json, so the thumbnail shows
 * what each design x palette looks like on music (the maintainer, Tue 6 Oct 2026).
 *
 * Source: seconds ${PREVIEW_START_S}..${PREVIEW_END_S} of the 24 s deck-row window in
 * tests/unit/fixtures/blocks-variance-anlz-window.json, each band downsampled by
 * per-bucket max over 2 points (as the server downsamples detail). ${PREVIEW_SECONDS} s
 * rather than 24 s so a card-wide preview shows single beats the way a deck row does.
 * Stored as PWV bytes (0..127, value = byte / 127), base64.
 * Regenerate from that fixture via scripts/write-deck-window-derived.mjs, never hand-edit.
 */
import type { AnlzWaveform } from '$lib/rb/anlz-types';

const BANDS = {
	low: '${encodeBand(bands.low)}',
	mid: '${encodeBand(bands.mid)}',
	high: '${encodeBand(bands.high)}'
} as const;

/** Points per band (${points}). */
export const PREVIEW_SAMPLE_POINTS = ${points};

/** Seconds of music the sample spans (shared with the preview beat grid). */
export { PREVIEW_SAMPLE_SECONDS } from './waveform-preview-beats';

function _decode(b64: string): number[] {
	const raw = atob(b64);
	const out = new Array<number>(raw.length);
	for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i) / 127;
	return out;
}

export function previewSampleBands(): { low: number[]; mid: number[]; high: number[] } {
	return { low: _decode(BANDS.low), mid: _decode(BANDS.mid), high: _decode(BANDS.high) };
}

/** The preview's waveform object. Built once per page: the deck painter caches
 * its per-waveform norms by identity, so repaints reuse them. */
let _sample: AnlzWaveform | undefined;

export function previewSampleWaveform(): AnlzWaveform {
	if (_sample === undefined) {
		const detail = { length: PREVIEW_SAMPLE_POINTS, ...previewSampleBands() };
		_sample = { kind: 'tri', preview: detail, detail };
	}
	return _sample;
}
`;

	const beatLines = beats.map((b) => `\t{ t: ${b.t}, n: ${b.n}, bpm: ${b.bpm} }`).join(',\n');
	const beatsFixed = `/**
 * The Settings waveform preview's beat grid (issue #10244): beats from
 * tests/unit/fixtures/blocks-variance-anlz-window.json in [window+${PREVIEW_START_S}, window+${PREVIEW_END_S}),
 * re-based to 0. Regenerate via scripts/write-deck-window-derived.mjs, never hand-edit.
 */
import type { AnlzBeat } from '$lib/rb/anlz-types';

/** Seconds of music the preview sample spans. */
export const PREVIEW_SAMPLE_SECONDS = ${PREVIEW_SECONDS};

export const PREVIEW_SAMPLE_BEATS: readonly AnlzBeat[] = [
${beatLines}
];
`;

	writeFileSync(samplePath, sampleSrc);
	writeFileSync(beatsPath, beatsFixed);
}

async function writeGolden() {
	const golden = await captureGothicGolden(new URL('gothic-blocks-clip-window.json', FIX));
	writeFileSync(new URL('gothic-blocks-pre-b5-golden.json', FIX), JSON.stringify(golden));
}

await writeGolden();
await writePreviewSample();
