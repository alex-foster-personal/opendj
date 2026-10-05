/**
 * Cue-marker painting and the wavestack palette (issue #877): per-kind fill
 * colours, loop cues as a spanning band, and the WCAG contrast floor those
 * markers must clear. Split out of render.ts (which owns the rest of the
 * row painters) purely to keep both files under the frontend size ratchet -
 * no behavior change, no new coupling.
 */
import type { AnlzCue, AnlzPhrase } from '$lib/rb/anlz-types';

/** Top strip reserved for beat ticks, cue markers and phrase chevrons.
 * Shared with render.ts's band painter so the marker strip and the band
 * area it carves out of the row stay in sync. */
export const MARKER_BAND_PX = 10;

/** Scale the opaque loop band for compact waveform surfaces. The main
 * wavestack keeps the 10px cap; a mini waveform reserves at most its top
 * third so the underlying waveform stays readable. */
export function markerBandHeightForSurface(surfaceHeightPx: number): number {
	if (!Number.isFinite(surfaceHeightPx) || surfaceHeightPx <= 0) {
		throw new RangeError('wave/cues: surfaceHeightPx must be finite and positive');
	}
	return Math.min(MARKER_BAND_PX, Math.max(1, Math.floor(surfaceHeightPx / 3)));
}

// ------------------------------------------------ contrast (WCAG 2.1)
//
// The same floor and formula bpm-heat.ts enforces for the BPM column
// (BPM_MIN_TEXT_CONTRAST, pin 7c0c0167cb0a), reused here per issue #877's
// acceptance criterion 6 for cue markers. Duplicated rather than imported:
// at the time of this PR the bpm-heat contrast helpers exist only on the
// still-open PR #890 (commit 982aeaa4), so importing them here would couple
// this branch to another in-flight one. Dedup once #890 lands - see
// .planning/debt/947.md.

/** WCAG 2.1 AA for body text; the same floor bpm-heat.ts enforces on the BPM
 * column. A cue marker is a small graphic, not text, but issue #877 asks
 * for "the same minimum contrast floor now enforced for the BPM column" -
 * reused verbatim rather than picked afresh. */
export const CUE_MIN_CONTRAST = 4.5;

type _Rgb = { r: number; g: number; b: number };

/** Parse '#rrggbb' or 'rgb(r g b)' / 'rgb(r, g, b)'. Throws rather than
 * defaulting: a colour this module cannot read is a bug in the caller. */
function _parseColor(color: string): _Rgb {
	const hex = /^#([0-9a-f]{6})$/i.exec(color.trim());
	if (hex !== null) {
		const n = parseInt(hex[1], 16);
		return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255 };
	}
	const fn = /^rgba?\(([^)]+)\)$/i.exec(color.trim());
	if (fn !== null) {
		const parts = fn[1]
			.split(/[\s,/]+/)
			.filter((p) => p !== '')
			.map(Number);
		if (parts.length >= 3 && parts.slice(0, 3).every((n) => Number.isFinite(n))) {
			return { r: parts[0], g: parts[1], b: parts[2] };
		}
	}
	throw new Error(`wave/cues contrast: cannot read colour ${color}`);
}

function _channelToLinear(value: number): number {
	const s = Math.min(255, Math.max(0, value)) / 255;
	return s <= 0.04045 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
}

/** WCAG relative luminance of a colour. */
export function relativeLuminance(color: string): number {
	const { r, g, b } = _parseColor(color);
	return 0.2126 * _channelToLinear(r) + 0.7152 * _channelToLinear(g) + 0.0722 * _channelToLinear(b);
}

/** WCAG 2.1 contrast ratio, 1..21. Symmetric in its arguments. */
export function contrastRatio(a: string, b: string): number {
	const la = relativeLuminance(a);
	const lb = relativeLuminance(b);
	const hi = Math.max(la, lb);
	const lo = Math.min(la, lb);
	return (hi + 0.05) / (lo + 0.05);
}

export interface WavePalette {
	/** Row background (--rb-bg). */
	bg: string;
	/** Decks 3/4 row and canvas fill (--rb-waverow-secondary). */
	secondaryBg: string;
	/** Lows band - dark blue in the default rekordbox 3Band palette
	 * (--rb-wave-low; issue #4219, orange under the legacy palette). */
	low: string;
	/** Mids band - amber by default (--rb-wave-mid). */
	mid: string;
	/** Highs band - white overlay by default (--rb-wave-high). */
	high: string;
	/** Single-color mono / line designs (--rb-wave-mono). */
	mono: string;
	/** Beat/bar ticks (--rb-text). */
	tick: string;
	/** Non-loop hot cue markers - green (--rb-green), the same colour
	 * HotCueBank.svelte paints a populated non-loop slot's letter. */
	cueHotCue: string;
	/** Loop markers (a hot-cue slot or a stored cue with out_ms set) -
	 * orange (--rb-orange), matching HotCueBank's loop-slot colour and
	 * reserving the orange/red hue family for loops (issue #877). */
	cueLoop: string;
	/** Memory cue markers (kind 'memory', no bank slot) - red (--rb-red);
	 * unchanged from the pre-#877 single cue colour. */
	cueMemory: string;
	/** Marker outline (--rb-text). Every cue marker is stroked with this
	 * regardless of fill: --rb-red alone measures 3.85:1 against --rb-bg,
	 * under the 4.5:1 WCAG AA floor issue #877 asks be reused from the BPM
	 * column (bpm-heat.ts BPM_MIN_TEXT_CONTRAST); the outline is what
	 * guarantees every marker clears it regardless of fill hue or theme. */
	cueOutline: string;
	/** Phrase chevrons (--rb-text-dim). */
	phrase: string;
	/** Vocal-presence bars (--rb-wave-vocal; mono palette: pale blue). */
	vocal: string;
}

// cueOutline intentionally reuses `tick` (--rb-text): rather than reading the
// same CSS var twice, it is derived below once the loop resolves it. cueLoop
// reads --rb-orange itself: it used to alias `low`, but issue #4219 moved the
// lows band to --rb-wave-low (dark blue by default) while loops stay orange.
const _PALETTE_VARS: Record<Exclude<keyof WavePalette, 'cueOutline'>, string> = {
	bg: '--rb-bg',
	secondaryBg: '--rb-waverow-secondary',
	low: '--rb-wave-low',
	mid: '--rb-wave-mid',
	high: '--rb-wave-high',
	mono: '--rb-wave-mono',
	tick: '--rb-text',
	cueHotCue: '--rb-green',
	cueLoop: '--rb-orange',
	cueMemory: '--rb-red',
	phrase: '--rb-text-dim',
	vocal: '--rb-wave-vocal'
};

/** Resolve the palette from the .perf-root CSS vars. Fail-fast: a missing
 * var means the element is outside .perf-root - that is a wiring bug. A
 * theme edit that drops cueOutline below the WCAG floor is the same class
 * of wiring bug (issue #877): every cue marker leans on that outline to
 * clear CUE_MIN_CONTRAST regardless of fill hue, so checking it here once
 * per palette resolve catches a theme regression at the source instead of
 * only in a pinned test. */
export function readPalette(el: HTMLElement): WavePalette {
	const styles = getComputedStyle(el);
	const out = {} as WavePalette;
	for (const key of Object.keys(_PALETTE_VARS) as (keyof typeof _PALETTE_VARS)[]) {
		const value = styles.getPropertyValue(_PALETTE_VARS[key]).trim();
		if (value === '') {
			throw new Error(
				`wavestack palette: CSS var ${_PALETTE_VARS[key]} empty - element not under .perf-root?`
			);
		}
		out[key] = value;
	}
	out.cueOutline = out.tick;
	const outlineContrast = contrastRatio(out.cueOutline, out.bg);
	if (outlineContrast < CUE_MIN_CONTRAST) {
		throw new Error(
			`wavestack palette: cueOutline (${out.cueOutline}) only clears ` +
				`${outlineContrast.toFixed(2)}:1 against bg (${out.bg}), below the ` +
				`${CUE_MIN_CONTRAST}:1 floor cue markers rely on`
		);
	}
	const secondaryOutlineContrast = contrastRatio(out.cueOutline, out.secondaryBg);
	if (secondaryOutlineContrast < CUE_MIN_CONTRAST) {
		throw new Error(
			`wavestack palette: cueOutline (${out.cueOutline}) only clears ` +
				`${secondaryOutlineContrast.toFixed(2)}:1 against secondaryBg (${out.secondaryBg}), below the ` +
				`${CUE_MIN_CONTRAST}:1 floor cue markers rely on`
		);
	}
	return out;
}

/** Fill colour for one cue marker, by kind (issue #877): loops (bank orange)
 * are kept out of the hot-cue/memory colours so the two read separably, per
 * slot letter, at a glance. */
function _cueFillColor(cue: AnlzCue, palette: WavePalette): string {
	if (cue.is_loop) return palette.cueLoop;
	if (cue.kind === 'hot_cue') return palette.cueHotCue;
	return palette.cueMemory;
}

/** Point marker (memory cue, or a non-loop hot cue): the original small
 * down-pointing triangle at the top edge, now filled per-kind and stroked
 * in `cueOutline` so it clears the contrast floor regardless of fill hue. */
function _drawPointCueMarker(
	ctx: CanvasRenderingContext2D,
	cue: AnlzCue,
	tLeft: number,
	pxPerS: number,
	w: number,
	palette: WavePalette
): void {
	const x = (cue.in_ms / 1000 - tLeft) * pxPerS;
	if (x < -4 || x > w + 4) return;
	ctx.beginPath();
	ctx.moveTo(x - 4, 0);
	ctx.lineTo(x + 4, 0);
	ctx.lineTo(x, 7);
	ctx.closePath();
	ctx.fillStyle = _cueFillColor(cue, palette);
	ctx.fill();
	ctx.lineWidth = 1;
	ctx.strokeStyle = palette.cueOutline;
	ctx.stroke();
}

/** Loop marker: a spanning band across the top marker strip from in_ms to
 * out_ms, not a point (issue #877 acceptance criterion 4). Deliberately
 * confined to MARKER_BAND_PX rather than the full row height so a STORED
 * loop cue reads distinctly from `drawLoopRegion`'s full-height translucent
 * band, which means "this loop is engaged right now". */
function _drawLoopCueMarker(
	ctx: CanvasRenderingContext2D,
	cue: AnlzCue,
	tLeft: number,
	pxPerS: number,
	w: number,
	palette: WavePalette,
	markerBandPx: number
): void {
	if (cue.out_ms === null) {
		throw new Error(`_drawLoopCueMarker: cue.is_loop true but out_ms is null (slot ${cue.slot})`);
	}
	const xIn = (cue.in_ms / 1000 - tLeft) * pxPerS;
	const xOut = (cue.out_ms / 1000 - tLeft) * pxPerS;
	if (xOut < -4 || xIn > w + 4) return;
	const left = Math.max(-4, xIn);
	const right = Math.min(w + 4, xOut);
	const bandWidth = Math.max(2, right - left);
	ctx.fillStyle = palette.cueLoop;
	ctx.fillRect(left, 0, bandWidth, markerBandPx);
	ctx.lineWidth = 1;
	ctx.strokeStyle = palette.cueOutline;
	ctx.strokeRect(
		left + 0.5,
		0.5,
		Math.max(0, bandWidth - 1),
		Math.max(0, markerBandPx - 1)
	);
}

/** Paint loop cues' spanning bands only. Split from `drawPointCueMarkers` so
 * a caller can run this as a BACKGROUND layer, before the beat grid and
 * phrase chevrons: a loop band is opaque across the full MARKER_BAND_PX
 * strip, so painting it on top of those (the pre-#877 combined single-pass
 * order) blanks out whatever beat/phrase marks fall under its span
 * (discussion_r3918219289). */
export function drawLoopCueBands(
	ctx: CanvasRenderingContext2D,
	cues: AnlzCue[],
	tLeft: number,
	pxPerS: number,
	w: number,
	palette: WavePalette,
	markerBandPx = MARKER_BAND_PX
): void {
	if (!Number.isFinite(markerBandPx) || markerBandPx <= 0 || markerBandPx > MARKER_BAND_PX) {
		throw new RangeError(`wave/cues: markerBandPx must be within 0..${MARKER_BAND_PX}`);
	}
	for (const cue of cues) {
		if (cue.is_loop) _drawLoopCueMarker(ctx, cue, tLeft, pxPerS, w, palette, markerBandPx);
	}
}

/** Paint non-loop cue markers (hot cue / memory) only - the FOREGROUND half
 * of the split with `drawLoopCueBands`, run after the beat grid and phrases
 * so a point marker still reads on top of them. */
export function drawPointCueMarkers(
	ctx: CanvasRenderingContext2D,
	cues: AnlzCue[],
	tLeft: number,
	pxPerS: number,
	w: number,
	palette: WavePalette
): void {
	for (const cue of cues) {
		if (!cue.is_loop) _drawPointCueMarker(ctx, cue, tLeft, pxPerS, w, palette);
	}
}

/** Paint real PSSI phrase boundaries as the same compact chevrons on every
 * waveform surface. Keeping this beside the cue painters gives the browser
 * preview and main wavestack one segmentation policy instead of two shapes
 * that can drift apart (LIBUX-12). */
export function drawPhraseMarkers(
	ctx: CanvasRenderingContext2D,
	phrases: AnlzPhrase[],
	tLeft: number,
	pxPerS: number,
	w: number,
	palette: WavePalette
): void {
	if (phrases.length === 0) return;
	ctx.strokeStyle = palette.phrase;
	ctx.lineWidth = 1.5;
	for (const phrase of phrases) {
		const x = (phrase.start_s - tLeft) * pxPerS;
		if (x < -6 || x > w + 6) continue;
		ctx.beginPath();
		ctx.moveTo(x, 1.5);
		ctx.lineTo(x + 4, 4.5);
		ctx.lineTo(x, 7.5);
		ctx.stroke();
	}
}
