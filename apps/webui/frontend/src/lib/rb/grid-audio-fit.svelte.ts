/**
 * Grid vs audio on the deck (GRIDFIT-03): does this track's beatgrid match its
 * music? The engine judges it (`GET /api/v1/beatgrid-flags/{id}/audio-fit`,
 * `apps/analysis_beatgrid/grid_audio_fit.py`), so the answer is identical in
 * the Tauri and Electron shells; this module only asks once per track and
 * keeps the answer.
 *
 * WHEN IT ASKS. Judging a track the engine has never judged costs it about
 * 1 to 2 s of one core (decode plus fold). A deck load is latency-sensitive
 * and shares that engine, so the request is made only after the deck's grid
 * has arrived and then after `REQUEST_DELAY_MS`: the warning is a few seconds
 * late, the load is never slower. A stored verdict comes back in milliseconds.
 *
 * WHAT IT SAYS. Only the verdicts a deck acts on (`deck_warning`: wrong BPM,
 * off-beat) produce a sentence; `shifted` is a library finding, never a deck
 * alarm (ADR-NEW-grid-audio-fit). A failed request is logged and says
 * nothing: no verdict is not a bad grid.
 *
 * CLI twin: `opendj track grid-fit <stable_id>`.
 */
import { API_BASE } from '$lib/api/base';

export const REQUEST_DELAY_MS = 2000;

export interface GridAudioFit {
	stable_id: string;
	verdict: string;
	reason: string | null;
	deck_warning: boolean;
	message: string | null;
	grid_bpm?: number | null;
	audio_bpm?: number | null;
	kick_offset_ms?: number | null;
}

type Entry = { state: 'pending' } | { state: 'done'; fit: GridAudioFit } | { state: 'failed' };

const _entries: Record<string, Entry> = $state({});

async function _request(stableId: string): Promise<void> {
	try {
		const response = await fetch(`${API_BASE}/api/v1/beatgrid-flags/${encodeURIComponent(stableId)}/audio-fit`);
		if (!response.ok) throw new Error(`HTTP ${response.status} ${await response.text()}`);
		_entries[stableId] = { state: 'done', fit: (await response.json()) as GridAudioFit };
	} catch (error: unknown) {
		_entries[stableId] = { state: 'failed' };
		console.error(`[grid-audio-fit] could not judge ${stableId}`, error);
	}
}

/** Ask the engine about this track once, after `REQUEST_DELAY_MS`. */
export function requestGridAudioFit(stableId: string): void {
	if (_entries[stableId] !== undefined) return;
	_entries[stableId] = { state: 'pending' };
	setTimeout(() => void _request(stableId), REQUEST_DELAY_MS);
}

/** The stored answer for a track, or null while unknown. Reactive. */
export function gridAudioFitFor(stableId: string | null): GridAudioFit | null {
	if (stableId === null) return null;
	const entry = _entries[stableId];
	return entry?.state === 'done' ? entry.fit : null;
}

/**
 * The sentence the BEAT SYNC warning shows: the audio-fit finding (when the
 * deck must act on it) before the grid-shape one, joined. Pure.
 */
export function deckGridWarningText(shapeWarning: string | null, fit: GridAudioFit | null): string | null {
	const audio = fit !== null && fit.deck_warning && fit.message ? fit.message : null;
	if (audio === null) return shapeWarning;
	if (shapeWarning === null) return audio;
	return `${audio} | ${shapeWarning}`;
}
