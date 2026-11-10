/**
 * ARTNUM-9649/9649b/10287: the persisted "Deck number on artwork" pref, its size
 * rule, and (ARTNUM-11719) the client-side artwork colour sampler behind the
 * glow and tinted modes.
 * When on, each deck's track artwork (deck header, waveform gutter, mixer
 * column) carries the deck number as a white outline with a dark halo, so
 * the art identifies the deck. Default off so nothing changes for an
 * existing user. Spec: (private path).
 *
 * Split out of prefs.svelte.ts the way section-overlay-prefs.ts is. Pure
 * module: no runes, so the unit suite and stories import it directly.
 */
export const DECK_NUMBER_ART_VALUES = ['off', 'halo', 'glow', 'tinted'] as const;
export type DeckNumberArt = (typeof DECK_NUMBER_ART_VALUES)[number];

export interface DeckNumberArtPrefsState {
	deck_number_art: DeckNumberArt;
}

export const DECK_NUMBER_ART_PREF_DEFAULTS: DeckNumberArtPrefsState = {
	deck_number_art: 'off'
};

/** Smallest live art square that still carries the overlay. The 22px
 * gutter (window under 800px tall) skips it: an 11px stroked glyph is mush. */
export const DECK_NUMBER_ART_MIN_PX = 40;

export function parseDeckNumberArt(value: unknown): DeckNumberArt | undefined {
	return (DECK_NUMBER_ART_VALUES as readonly unknown[]).includes(value)
		? (value as DeckNumberArt)
		: undefined;
}

/** Parse or throw, for the typed bus command and the settings apply path. */
export function requireDeckNumberArt(value: unknown, field: string): DeckNumberArt {
	const mode = parseDeckNumberArt(value);
	if (mode === undefined)
		throw new TypeError(
			`${field} must be ${DECK_NUMBER_ART_VALUES.join('|')}; got ${String(value)}`
		);
	return mode;
}

/** Validate the stored blob: absent keeps the default, anything else bad throws. */
export function validateDeckNumberArtPrefFields(
	parsed: { deck_number_art?: unknown },
	storageKey: string
): Partial<DeckNumberArtPrefsState> {
	if (parsed.deck_number_art === undefined) return {};
	const mode = parseDeckNumberArt(parsed.deck_number_art);
	if (mode === undefined) {
		throw new Error(
			`${storageKey}: malformed prefs blob (deck_number_art must be ${DECK_NUMBER_ART_VALUES.join('|')}) - ` +
				'clear the localStorage key to recover'
		);
	}
	return { deck_number_art: mode };
}

/** True when the overlay draws on an art square of sizePx CSS pixels. */
export function deckNumberArtVisible(mode: DeckNumberArt, sizePx: number): boolean {
	return mode !== 'off' && sizePx >= DECK_NUMBER_ART_MIN_PX;
}

// ---------------------------------------------------------------------------
// Quality signal (spec: Proposed quality signal). WCAG contrast between the
// white stroke and the 90th-percentile relative luminance of the art under the
// glyph box. The halo is modeled as darkening the art to 0.35 of its luminance,
// the same model the spec measured (0 of 15 covers under 3:1, minimum 3.05).

/** WCAG non-text contrast floor, mimicked. */
export const DECK_NUMBER_ART_MIN_CONTRAST = 3;
/** Modeled effect of the dark halo on the art under the glyph. */
export const DECK_NUMBER_ART_HALO_LUMINANCE_FACTOR = 0.35;

/** WCAG 2 contrast ratio between two relative luminances in 0..1. */
export function wcagContrast(a: number, b: number): number {
	if (!(a >= 0 && a <= 1 && b >= 0 && b <= 1))
		throw new RangeError(`luminance out of 0..1: ${a}, ${b}`);
	return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}

/** What sits directly behind the white stroke. The outer glow sits outside the
 * stroke, so it does not change the art under the glyph. */
export type StrokeBacking = 'none' | 'dark-halo' | 'tinted-halo';

/** Ceiling on the relative luminance of the tinted halo's shade of the art colour. */
export const DECK_NUMBER_ART_TINT_HALO_MAX_LUMINANCE = 0.008;

export function strokeBackingOf(mode: Exclude<DeckNumberArt, 'off'>): StrokeBacking {
	switch (mode) {
		case 'halo':
		case 'glow':
			return 'dark-halo';
		case 'tinted':
			return 'tinted-halo';
		default: {
			const _exhaustive: never = mode;
			throw new Error(`Unhandled deck_number_art: ${String(_exhaustive)}`);
		}
	}
}

/** Contrast of the white stroke over art whose p90 luminance is artP90, for a backing. */
export function strokeContrast(artP90: number, backing: StrokeBacking): number {
	const k = DECK_NUMBER_ART_HALO_LUMINANCE_FACTOR;
	switch (backing) {
		case 'none':
			return wcagContrast(1, artP90);
		case 'dark-halo':
			return wcagContrast(1, artP90 * k);
		case 'tinted-halo':
			// Worst case: the shaded halo is as bright as its cap.
			return wcagContrast(1, artP90 * k + (1 - k) * DECK_NUMBER_ART_TINT_HALO_MAX_LUMINANCE);
		default: {
			const _exhaustive: never = backing;
			throw new Error(`Unhandled stroke backing: ${String(_exhaustive)}`);
		}
	}
}

// ---------------------------------------------------------------------------
// Glow geometry: mimicked from Material's text-on-image scrim guidance, as the
// halo is. Fractions of the art square.

export const DECK_NUMBER_ART_GLOW_RADIUS = 0.2;
export const DECK_NUMBER_ART_GLOW_ALPHA = 0.55;

// ---------------------------------------------------------------------------
// Dominant colour sampler (ARTNUM-11719). Draw the already loaded cover once
// into an 8x8 canvas, take the most saturated of the four most frequent colour
// bins, ignoring near-black, near-white and transparent cells. Cached per
// stable_id; no request. /artwork is same-origin, so the canvas is not
// tainted; a tainted one (remote cover) yields null and the mode falls back to halo.

export interface Rgb {
	r: number;
	g: number;
	b: number;
}

export const DECK_NUMBER_ART_SAMPLE_PX = 8;
export const DECK_NUMBER_ART_NEAR_BLACK_MAX = 40;
export const DECK_NUMBER_ART_NEAR_WHITE_MIN = 215;
export const DECK_NUMBER_ART_CACHE_MAX = 128;
export const DECK_NUMBER_ART_RESAMPLE_MS = 1000;

interface ColorBin {
	count: number;
	sum: Rgb;
}

function _saturation(c: Rgb): number {
	const max = Math.max(c.r, c.g, c.b);
	return max === 0 ? 0 : (max - Math.min(c.r, c.g, c.b)) / max;
}

/** Pure core: dominant colour of an 8x8 RGBA pixel buffer, or null when every cell is ignored. */
export function dominantColorFromPixels(rgba: ArrayLike<number>): Rgb | null {
	const cells = DECK_NUMBER_ART_SAMPLE_PX * DECK_NUMBER_ART_SAMPLE_PX;
	if (rgba.length !== cells * 4)
		throw new RangeError(`expected ${cells * 4} RGBA values, got ${rgba.length}`);
	const bins = new Map<number, ColorBin>();
	for (let i = 0; i < rgba.length; i += 4) {
		const r = rgba[i],
			g = rgba[i + 1],
			b = rgba[i + 2],
			a = rgba[i + 3];
		if (a < 128) continue;
		if (Math.max(r, g, b) < DECK_NUMBER_ART_NEAR_BLACK_MAX) continue;
		if (Math.min(r, g, b) > DECK_NUMBER_ART_NEAR_WHITE_MIN) continue;
		const key = ((r >> 5) << 6) | ((g >> 5) << 3) | (b >> 5);
		const bin = bins.get(key) ?? { count: 0, sum: { r: 0, g: 0, b: 0 } };
		bin.count += 1;
		bin.sum.r += r;
		bin.sum.g += g;
		bin.sum.b += b;
		bins.set(key, bin);
	}
	// Stable sort keeps first occurrence on count ties.
	const top = [...bins.values()].sort((x, y) => y.count - x.count).slice(0, 4);
	let best: { color: Rgb; count: number; sat: number } | null = null;
	for (const bin of top) {
		const color = {
			r: Math.round(bin.sum.r / bin.count),
			g: Math.round(bin.sum.g / bin.count),
			b: Math.round(bin.sum.b / bin.count)
		};
		const sat = _saturation(color);
		if (best === null || sat > best.sat || (sat === best.sat && bin.count > best.count))
			best = { color, count: bin.count, sat };
	}
	return best === null ? null : best.color;
}

interface SamplerContext {
	drawImage(media: CanvasImageSource, dx: number, dy: number, dw: number, dh: number): void;
	getImageData(sx: number, sy: number, sw: number, sh: number): { data: ArrayLike<number> };
}

export interface SamplerCanvas {
	width: number;
	height: number;
	getContext(id: '2d', opts: { willReadFrequently: boolean }): SamplerContext | null;
}

const _defaultCanvasFactory = (): SamplerCanvas =>
	document.createElement('canvas') as unknown as SamplerCanvas;

let _canvasFactory: () => SamplerCanvas = _defaultCanvasFactory;
let _samplerCtx: SamplerContext | null = null;
const _tintCache = new Map<string, Rgb | null>();

/** Test seam: replace the canvas the sampler draws into. */
export function setDeckNumberArtCanvasFactory(factory: () => SamplerCanvas): void {
	_canvasFactory = factory;
	_samplerCtx = null;
}

/** Test seam: clear the cache, the reused canvas and the factory. */
export function resetDeckNumberArtSampler(): void {
	_tintCache.clear();
	_samplerCtx = null;
	_canvasFactory = _defaultCanvasFactory;
}

function _samplerContext(): SamplerContext {
	if (_samplerCtx !== null) return _samplerCtx;
	const canvas = _canvasFactory();
	canvas.width = DECK_NUMBER_ART_SAMPLE_PX;
	canvas.height = DECK_NUMBER_ART_SAMPLE_PX;
	const ctx = canvas.getContext('2d', { willReadFrequently: true });
	if (ctx === null) throw new Error('deck-number-art: 2d canvas context unavailable');
	_samplerCtx = ctx;
	return ctx;
}

function _cacheTint(stableId: string, tint: Rgb | null): void {
	_tintCache.delete(stableId);
	_tintCache.set(stableId, tint);
	if (_tintCache.size > DECK_NUMBER_ART_CACHE_MAX) {
		const oldest = _tintCache.keys().next().value as string;
		_tintCache.delete(oldest);
	}
}

/** Dominant colour of a loaded cover, cached per stable_id; null when unreadable (tainted). */
export function dominantColorOf(
	media: CanvasImageSource,
	stableId: string,
	opts: { refresh: boolean } = { refresh: false }
): Rgb | null {
	if (!opts.refresh && _tintCache.has(stableId)) return _tintCache.get(stableId) ?? null;
	const ctx = _samplerContext();
	const px = DECK_NUMBER_ART_SAMPLE_PX;
	let tint: Rgb | null;
	try {
		ctx.drawImage(media, 0, 0, px, px);
		tint = dominantColorFromPixels(ctx.getImageData(0, 0, px, px).data);
	} catch (err) {
		if (!(err instanceof DOMException && err.name === 'SecurityError')) throw err;
		tint = null;
	}
	_cacheTint(stableId, tint);
	return tint;
}

/** glow and tinted need a colour; with none (tainted or not sampled yet) they draw as halo. */
export function effectiveDeckNumberArt(mode: DeckNumberArt, tint: Rgb | null): DeckNumberArt {
	return (mode === 'glow' || mode === 'tinted') && tint === null ? 'halo' : mode;
}

/**
 * CSS grayscale(1) on sRGB values. Gothic (mono-dev) and Light filter /artwork
 * images to grayscale (theme.css), so the tint follows the filtered colour and
 * tinted renders as a neutral glow there; the colour returns on hover with the
 * art's own 120ms transition. contrast(1.05) is ignored (sub-1% change).
 */
export function grayscaleOf(rgb: Rgb): Rgb {
	const v = Math.round(0.2126 * rgb.r + 0.7152 * rgb.g + 0.0722 * rgb.b);
	return { r: v, g: v, b: v };
}

function _toLinear(c: number): number {
	const s = c / 255;
	return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
}

function _toSrgb(l: number): number {
	const s = l <= 0.0031308 ? l * 12.92 : 1.055 * l ** (1 / 2.4) - 0.055;
	return Math.max(0, Math.min(255, Math.floor(s * 255)));
}

/** Shade of rgb, scaled in linear light, whose luminance is at most the tinted-halo cap. */
export function shadeForHalo(rgb: Rgb): Rgb {
	const lin = [_toLinear(rgb.r), _toLinear(rgb.g), _toLinear(rgb.b)];
	const lum = 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2];
	if (lum <= DECK_NUMBER_ART_TINT_HALO_MAX_LUMINANCE) return rgb;
	const k = DECK_NUMBER_ART_TINT_HALO_MAX_LUMINANCE / lum;
	return { r: _toSrgb(lin[0] * k), g: _toSrgb(lin[1] * k), b: _toSrgb(lin[2] * k) };
}

const _css = (c: Rgb): string => `rgb(${c.r} ${c.g} ${c.b})`;

/** CSS custom properties for the glow layers, colour and grayscale-skin variants. */
export function deckNumberArtGlowVars(mode: 'glow' | 'tinted', tint: Rgb): Record<string, string> {
	const halo = mode === 'tinted' ? shadeForHalo(tint) : { r: 0, g: 0, b: 0 };
	return {
		'--dna-glow': _css(tint),
		'--dna-glow-gray': _css(grayscaleOf(tint)),
		'--dna-halo': _css(halo),
		'--dna-halo-gray': _css(grayscaleOf(halo))
	};
}

/** A cover whose pixels change while it plays (a video, or an img marked animated). */
export function isAnimatedCover(media: Pick<Element, 'tagName' | 'hasAttribute'>): boolean {
	return media.tagName === 'VIDEO' || media.hasAttribute('data-animated-cover');
}

/** Resample an animated cover every intervalMs while the deck plays; idle when paused. */
export function createTintResampler(
	sample: () => void,
	intervalMs: number
): { setPlaying(playing: boolean): void; stop(): void } {
	let handle: ReturnType<typeof setInterval> | null = null;
	const stop = (): void => {
		if (handle !== null) clearInterval(handle);
		handle = null;
	};
	return {
		setPlaying(playing: boolean): void {
			if (!playing) stop();
			else if (handle === null) handle = setInterval(sample, intervalMs);
		},
		stop
	};
}
