/**
 * Main-waveform zoom (WAVEZOOM-6802..06): the bar ladder and the stored prefs.
 * The window each step shows, the playhead and the glide live in
 * wave-zoom-view.ts, which only the waveform row loads, so this module (on
 * the "/" first-paint path through prefs) stays small. Pure, unit tested.
 *
 * Design: https://claude.ai/artifact/DTqYUgGB3HGPF7dokZUSQF (Thu 8 Oct 2026),
 * defaults taken Thu 15 Oct 2026: 16 bars, elastic playhead, one shared zoom.
 */
import type { DeckId } from '$lib/player/constants';

export type WaveZoom = '4' | '8' | '16' | '32' | '64' | 'whole';

/** Most zoomed in first. */
export const WAVE_ZOOM_LADDER: readonly WaveZoom[] = ['4', '8', '16', '32', '64', 'whole'];
export const WAVE_ZOOM_DEFAULT: WaveZoom = '16';

/** 'elastic' (design option A, default): centered until the window is wider
 * than half the reference track, then glides so the whole track fits.
 * 'center': the playhead never leaves the middle (the pre-zoom behavior). */
export type WavePlayheadMode = 'elastic' | 'center';
export const WAVE_PLAYHEAD_MODES: readonly WavePlayheadMode[] = ['elastic', 'center'];
export const WAVE_PLAYHEAD_DEFAULT: WavePlayheadMode = 'elastic';

export function parseWaveZoom(raw: unknown): WaveZoom | undefined {
	if (raw === undefined) return undefined;
	if (!WAVE_ZOOM_LADDER.includes(raw as WaveZoom)) {
		throw new Error(`wave_zoom must be ${WAVE_ZOOM_LADDER.join('|')}, got ${String(raw)}`);
	}
	return raw as WaveZoom;
}

export function parseWavePlayheadMode(raw: unknown): WavePlayheadMode | undefined {
	if (raw === undefined) return undefined;
	if (!WAVE_PLAYHEAD_MODES.includes(raw as WavePlayheadMode)) {
		throw new Error(`wave_zoom_playhead must be ${WAVE_PLAYHEAD_MODES.join('|')}, got ${String(raw)}`);
	}
	return raw as WavePlayheadMode;
}

/** +1 zooms in (fewer bars), -1 zooms out. Stops at either end. */
export function stepWaveZoom(zoom: WaveZoom, direction: 1 | -1): WaveZoom {
	const i = WAVE_ZOOM_LADDER.indexOf(zoom);
	const next = Math.min(WAVE_ZOOM_LADDER.length - 1, Math.max(0, i - direction));
	return WAVE_ZOOM_LADDER[next];
}

export interface WaveZoomPrefsState {
	/** The shared zoom step for every deck. */
	wave_zoom: WaveZoom;
	/** Where the playhead sits once zoomed far out. */
	wave_zoom_playhead: WavePlayheadMode;
	/** true (default): one zoom for all decks. false: each deck keeps its own
	 * step in wave_zoom_decks, and wheel or keys zoom only that deck. */
	wave_zoom_linked: boolean;
	wave_zoom_decks: Partial<Record<`${DeckId}`, WaveZoom>>;
}

export const WAVE_ZOOM_PREF_DEFAULTS: WaveZoomPrefsState = {
	wave_zoom: WAVE_ZOOM_DEFAULT,
	wave_zoom_playhead: WAVE_PLAYHEAD_DEFAULT,
	wave_zoom_linked: true,
	wave_zoom_decks: {}
};

/** The step a deck shows: the shared one when linked, else its own (falling
 * back to the shared one until it has been zoomed on its own). */
export function deckWaveZoom(prefs: WaveZoomPrefsState, deck: DeckId): WaveZoom {
	if (prefs.wave_zoom_linked) return prefs.wave_zoom;
	return prefs.wave_zoom_decks[`${deck}`] ?? prefs.wave_zoom;
}

/** Validate the stored blob's zoom keys: absent keeps the default; present
 * but malformed throws with the storage key named, like every other pref. */
export function validateWaveZoomPrefFields(
	parsed: Record<string, unknown>,
	storageKey: string
): Partial<WaveZoomPrefsState> {
	const out: Partial<WaveZoomPrefsState> = {};
	const fail = (what: string): never => {
		throw new Error(`${storageKey}: malformed prefs blob (${what}) - clear the localStorage key to recover`);
	};
	try {
		const zoom = parseWaveZoom(parsed.wave_zoom);
		if (zoom !== undefined) out.wave_zoom = zoom;
		const mode = parseWavePlayheadMode(parsed.wave_zoom_playhead);
		if (mode !== undefined) out.wave_zoom_playhead = mode;
	} catch (error) {
		fail((error as Error).message);
	}
	if (parsed.wave_zoom_linked !== undefined) {
		if (typeof parsed.wave_zoom_linked !== 'boolean') fail('wave_zoom_linked is not a boolean');
		out.wave_zoom_linked = parsed.wave_zoom_linked as boolean;
	}
	if (parsed.wave_zoom_decks !== undefined) {
		const raw = parsed.wave_zoom_decks;
		if (raw === null || typeof raw !== 'object' || Array.isArray(raw)) fail('wave_zoom_decks is not an object');
		const decks: WaveZoomPrefsState['wave_zoom_decks'] = {};
		for (const [key, value] of Object.entries(raw as Record<string, unknown>)) {
			if (!['1', '2', '3', '4'].includes(key)) fail(`wave_zoom_decks has unknown deck ${key}`);
			try {
				const zoom = parseWaveZoom(value);
				if (zoom !== undefined) decks[key as `${DeckId}`] = zoom;
			} catch (error) {
				fail((error as Error).message);
			}
		}
		out.wave_zoom_decks = decks;
	}
	return out;
}
