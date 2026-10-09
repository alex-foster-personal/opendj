/**
 * STRUCT-05 section overlay: each analyzed section (a rekordbox PSSI phrase
 * or an own section) painted as a translucent colored band with its label,
 * behind the beat grid and cues, on every waveform surface that opts in via
 * the `show_sections` pref.
 *
 * Two halves, deliberately separated for the per-frame budget:
 *   1. `sectionSpans(phrases)` turns an `/anlz` phrases array into display
 *      spans (label text, color family, ambient flag). It runs ONCE per
 *      phrases array identity (WeakMap memo), never per frame.
 *   2. `drawSectionBands(...)` paints those spans. It runs inside the
 *      wavestack rAF loop, so it allocates nothing: no arrays, objects,
 *      closures or strings per frame, and it visits only the visible spans
 *      (binary search to the first one, stop past the right edge).
 *
 * Rendering honesty: a band paints only for a section the analysis actually
 * returned. An unknown PSSI kind/mood or an unrecognized own label renders
 * in the neutral family with an honest "Phrase <kind>" / raw label, never a
 * guessed name.
 */
import type { AnlzPhrase } from '$lib/rb/anlz-types';

// ------------------------------------------------------------- families

// The family list and token reader live in section-palette.ts, which cues.ts
// imports statically for every palette read; this module itself loads lazily
// (sections-lazy.svelte.ts) so it stays out of the "/" first-paint bundle.
import type { SectionFamily, SectionPalette } from './section-palette';
export { SECTION_FAMILIES, readSectionPalette } from './section-palette';
export type { SectionFamily, SectionPalette } from './section-palette';

/**
 * Label word -> family. Own sections today can carry pop words (intro,
 * verse, chorus, inst, solo, break, bridge, outro, start, end) and are moving
 * to DJ vocabulary (intro, buildup, drop, breakdown, cooldown, outro); the
 * rekordbox PSSI names (below) reduce to their first word (Up, Down,
 * Verse 3, ...). The mood-1 mapping follows
 * (private path)
 * "Mapping label vocabularies onto rekordbox PSSI": Up -> buildup,
 * Down -> breakdown/cooldown, Chorus -> drop.
 */
const _FAMILY_BY_WORD: ReadonlyMap<string, SectionFamily> = new Map<string, SectionFamily>([
	['intro', 'edge'],
	['outro', 'edge'],
	['start', 'edge'],
	['end', 'edge'],
	['buildup', 'rise'],
	['build', 'rise'],
	['build-up', 'rise'],
	['up', 'rise'],
	['pre-chorus', 'rise'],
	['prechorus', 'rise'],
	['drop', 'drop'],
	['chorus', 'drop'],
	['breakdown', 'calm'],
	['cooldown', 'calm'],
	['break', 'calm'],
	['down', 'calm'],
	['verse', 'body'],
	['bridge', 'body'],
	['inst', 'body'],
	['solo', 'body']
]);

/** Color family for a section label; unknown words fall back to neutral. */
export function sectionFamilyForLabel(label: string): SectionFamily {
	const word = label.trim().toLowerCase().split(/[\s_]+/)[0] ?? '';
	return _FAMILY_BY_WORD.get(word) ?? 'neutral';
}

// ------------------------------------------------------ rekordbox PSSI

/** PSSI header `mood` values (1 high, 2 mid, 3 low). */
export const PSSI_MOOD_HIGH = 1;
export const PSSI_MOOD_MID = 2;
export const PSSI_MOOD_LOW = 3;

// Phrase kind ids per mood, verified against Deep-Symmetry crate-digger
// src/main/kaitai/rekordbox_anlz.ksy enums mood_high_phrase /
// mood_mid_phrase / mood_low_phrase (fetched Wed 30 Sep 2026) and
// (private path)
// Table "Moods and phrase labels". pyrekordbox 0.4.4 (anlz/structs.py
// SongStructureEntry) parses `kind` as a bare Int16ub and carries no name
// table of its own. Low mood has three ids each that rekordbox displays as
// "Verse 1" and "Verse 2" (crate-digger: verse_1b/1c, verse_2b/2c). High mood
// numbered variants (Intro 1/2, Up 1/2/3, ...) live in the k1/k2/k3 flag
// bytes, which the /anlz payload does not carry, so they render unnumbered.
const _PSSI_HIGH: Readonly<Record<number, string>> = {
	1: 'Intro',
	2: 'Up',
	3: 'Down',
	5: 'Chorus',
	6: 'Outro'
};
const _PSSI_MID: Readonly<Record<number, string>> = {
	1: 'Intro',
	2: 'Verse 1',
	3: 'Verse 2',
	4: 'Verse 3',
	5: 'Verse 4',
	6: 'Verse 5',
	7: 'Verse 6',
	8: 'Bridge',
	9: 'Chorus',
	10: 'Outro'
};
const _PSSI_LOW: Readonly<Record<number, string>> = {
	1: 'Intro',
	2: 'Verse 1',
	3: 'Verse 1',
	4: 'Verse 1',
	5: 'Verse 2',
	6: 'Verse 2',
	7: 'Verse 2',
	8: 'Bridge',
	9: 'Chorus',
	10: 'Outro'
};

/** rekordbox's display name for a PSSI phrase, or null when the mood or the
 * kind id is not one the format documents (never a guessed name). */
export function pssiPhraseName(mood: number, kind: number): string | null {
	const table =
		mood === PSSI_MOOD_HIGH
			? _PSSI_HIGH
			: mood === PSSI_MOOD_MID
				? _PSSI_MID
				: mood === PSSI_MOOD_LOW
					? _PSSI_LOW
					: null;
	if (table === null) return null;
	return table[kind] ?? null;
}

// ------------------------------------------------------- display spans

/** One section ready to paint. Built once per phrases array. */
export interface SectionSpan {
	start_s: number;
	end_s: number;
	/** Display text, e.g. "Drop", "Verse 2", "Phrase 4". */
	label: string;
	family: SectionFamily;
	/** Own-section ambient intro/outro: painted lighter and hatched. */
	ambient: boolean;
	/** Label width in px at SECTION_LABEL_FONT; -1 until first measured.
	 * Cached here so the rAF loop calls measureText once per span, not
	 * once per frame. */
	labelWidthPx: number;
}

function _displayLabel(raw: string): string {
	const text = raw.trim().replace(/_/g, ' ');
	if (text === '') return 'Section';
	return text.charAt(0).toUpperCase() + text.slice(1);
}

function _spanOf(phrase: AnlzPhrase): SectionSpan {
	if (!Number.isFinite(phrase.start_s) || !Number.isFinite(phrase.end_s)) {
		throw new RangeError(
			`sectionSpans: phrase bounds must be finite, got ${phrase.start_s}..${phrase.end_s}`
		);
	}
	let label: string;
	let family: SectionFamily;
	if (typeof phrase.label === 'string') {
		label = _displayLabel(phrase.label);
		family = sectionFamilyForLabel(phrase.label);
	} else {
		const name = pssiPhraseName(phrase.mood, phrase.kind);
		label = name ?? `Phrase ${phrase.kind}`;
		family = name === null ? 'neutral' : sectionFamilyForLabel(name);
	}
	return {
		start_s: phrase.start_s,
		end_s: phrase.end_s,
		label,
		family,
		ambient: phrase.ambient === true,
		labelWidthPx: -1
	};
}

const _spanCache = new WeakMap<readonly AnlzPhrase[], readonly SectionSpan[]>();

/** Display spans for a phrases array, sorted by start, zero-width sections
 * dropped. Memoized by the array's identity: the anlz payload is immutable
 * once fetched, so a new payload is a new array and a fresh build. */
export function sectionSpans(phrases: readonly AnlzPhrase[]): readonly SectionSpan[] {
	const cached = _spanCache.get(phrases);
	if (cached !== undefined) return cached;
	const spans = phrases
		.map(_spanOf)
		.filter((s) => s.end_s > s.start_s)
		.sort((a, b) => a.start_s - b.start_s);
	_spanCache.set(phrases, spans);
	return spans;
}

// ------------------------------------------------------------- painter

/** Full-height body alpha: low so the waveform stays fully readable. */
export const SECTION_FILL_ALPHA = 0.16;
/** Ambient intro/outro body alpha: lighter, plus hatching. */
export const SECTION_AMBIENT_FILL_ALPHA = 0.08;
/** Stronger tint across the top label strip so the label reads. */
export const SECTION_HEADER_ALPHA = 0.4;
/** Section-start edge line alpha. */
export const SECTION_EDGE_ALPHA = 0.55;
const _HATCH_ALPHA = 0.3;
const _HATCH_STEP_PX = 6;
const _LABEL_ALPHA = 0.95;
/** Label text left pad: clears the 4px phrase chevron at the section start. */
const _LABEL_PAD_PX = 6;
/** Skip the label entirely when less than this much band is visible. */
export const SECTION_MIN_LABEL_PX = 18;
export const SECTION_LABEL_FONT = '700 8px sans-serif';

/** Index of the span that contains or follows `t`. Spans are a partition
 * of the track (sorted, non-overlapping), so the span containing `t` is
 * the last one starting at or before it. */
function _firstVisible(spans: readonly SectionSpan[], t: number): number {
	let lo = 0;
	let hi = spans.length;
	while (lo < hi) {
		const mid = (lo + hi) >> 1;
		if (spans[mid].start_s <= t) lo = mid + 1;
		else hi = mid;
	}
	return lo > 0 ? lo - 1 : 0;
}

function _hatch(
	ctx: CanvasRenderingContext2D,
	x0: number,
	left: number,
	right: number,
	h: number,
	color: string
): void {
	ctx.save();
	ctx.beginPath();
	ctx.rect(left, 0, right - left, h);
	ctx.clip();
	ctx.beginPath();
	// Phase anchored on the section start so the hatch scrolls with the track.
	let x = x0 + Math.floor((left - h - x0) / _HATCH_STEP_PX) * _HATCH_STEP_PX;
	for (; x < right; x += _HATCH_STEP_PX) {
		ctx.moveTo(x, h);
		ctx.lineTo(x + h, 0);
	}
	ctx.globalAlpha = _HATCH_ALPHA;
	ctx.strokeStyle = color;
	ctx.lineWidth = 1;
	ctx.stroke();
	ctx.restore();
}

function _label(
	ctx: CanvasRenderingContext2D,
	span: SectionSpan,
	textX: number,
	right: number,
	labelBandPx: number,
	color: string
): void {
	const room = right - textX - 2;
	if (room < SECTION_MIN_LABEL_PX) return;
	if (span.labelWidthPx < 0) span.labelWidthPx = ctx.measureText(span.label).width;
	ctx.fillStyle = color;
	const baseline = labelBandPx - 2;
	if (span.labelWidthPx <= room) {
		ctx.fillText(span.label, textX, baseline);
		return;
	}
	ctx.save();
	ctx.beginPath();
	ctx.rect(textX, 0, room, labelBandPx);
	ctx.clip();
	ctx.fillText(span.label, textX, baseline);
	ctx.restore();
}

/**
 * Paint the visible section BODIES: a full-height low-alpha tint, a start
 * edge, the hatch for ambient sections and, when `labelBandPx` > 0, a
 * stronger tint across the top marker strip the labels sit on. No text: see
 * drawSectionLabels. Coordinates: x = (t - tLeft) * pxPerS, so a scrolling
 * view passes its window and a whole-track surface (or the wavestack's
 * cached full-track band image) passes tLeft 0. Callers run this BEFORE the
 * waveform, beat grid and cues so every one of those paints on top of it.
 */
export function drawSectionBands(
	ctx: CanvasRenderingContext2D,
	spans: readonly SectionSpan[],
	tLeft: number,
	pxPerS: number,
	w: number,
	h: number,
	palette: SectionPalette,
	labelBandPx: number
): void {
	const n = spans.length;
	if (n === 0 || !(pxPerS > 0) || w <= 0 || h <= 0) return;
	const tRight = tLeft + w / pxPerS;
	for (let i = _firstVisible(spans, tLeft); i < n; i++) {
		const span = spans[i];
		if (span.start_s >= tRight) break;
		const x0 = (span.start_s - tLeft) * pxPerS;
		const x1 = (span.end_s - tLeft) * pxPerS;
		if (x1 <= 0) continue;
		const left = Math.max(0, x0);
		const right = Math.min(w, x1);
		if (right - left < 0.5) continue;
		const color = palette[span.family];
		ctx.fillStyle = color;
		ctx.globalAlpha = span.ambient ? SECTION_AMBIENT_FILL_ALPHA : SECTION_FILL_ALPHA;
		ctx.fillRect(left, 0, right - left, h);
		if (labelBandPx > 0) {
			ctx.globalAlpha = span.ambient ? SECTION_HEADER_ALPHA / 2 : SECTION_HEADER_ALPHA;
			ctx.fillRect(left, 0, right - left, labelBandPx);
		}
		if (span.ambient) _hatch(ctx, x0, left, right, h, color);
		if (x0 >= 0) {
			ctx.globalAlpha = SECTION_EDGE_ALPHA;
			ctx.fillStyle = color;
			ctx.fillRect(x0, 0, 1, h);
		}
	}
	ctx.globalAlpha = 1;
}

/**
 * Paint the visible section LABELS in the top marker strip, each at its
 * section start, or pinned just inside the left edge once that start has
 * scrolled off (so a 24 s window inside a 60 s drop still says "Drop").
 * Clipped to the band; skipped when less than SECTION_MIN_LABEL_PX of band
 * is left. Runs per frame on the destination canvas (never baked into the
 * wavestack's CSS-px band image, which the DPR transform would blur):
 * O(visible sections), no allocation, measureText once per span ever.
 */
export function drawSectionLabels(
	ctx: CanvasRenderingContext2D,
	spans: readonly SectionSpan[],
	tLeft: number,
	pxPerS: number,
	w: number,
	palette: SectionPalette,
	labelBandPx: number
): void {
	const n = spans.length;
	if (n === 0 || !(pxPerS > 0) || w <= 0 || labelBandPx <= 0) return;
	const tRight = tLeft + w / pxPerS;
	ctx.font = SECTION_LABEL_FONT;
	ctx.textBaseline = 'alphabetic';
	ctx.globalAlpha = _LABEL_ALPHA;
	for (let i = _firstVisible(spans, tLeft); i < n; i++) {
		const span = spans[i];
		if (span.start_s >= tRight) break;
		const x1 = (span.end_s - tLeft) * pxPerS;
		if (x1 <= 0) continue;
		const left = Math.max(0, (span.start_s - tLeft) * pxPerS);
		_label(ctx, span, left + _LABEL_PAD_PX, Math.min(w, x1), labelBandPx, palette.text);
	}
	ctx.globalAlpha = 1;
}
