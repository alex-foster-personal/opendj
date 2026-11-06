/**
 * Grid vs audio on the deck (GRIDFIT-03): does this track's beatgrid match its
 * music? The engine judges it (`GET /api/v1/beatgrid-flags/{id}/audio-fit`,
 * `apps/analysis_beatgrid/grid_audio_fit.py`), so the answer is identical in
 * the Tauri and Electron shells; this module asks once per successful verdict
 * per page session and keeps the answer. A `failed` entry is asked again on
 * the next `requestGridAudioFit`.
 *
 * WHEN IT ASKS. Judging a track the engine has never judged costs it about
 * 1 to 2 s of one core (decode plus fold). A deck load is latency-sensitive
 * and shares that engine, so the request is made only after the deck's grid
 * has arrived and then after `REQUEST_DELAY_MS`: the warning is a few seconds
 * late, the load is never slower. A stored verdict comes back in milliseconds.
 * The function returns a cancel: the last waiter that cancels before the
 * delay elapses drops the pending entry and the engine is not asked.
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
const _timers = new Map<string, ReturnType<typeof setTimeout>>();
const _waiters = new Map<string, number>();

async function _request(stableId: string): Promise<void> {
	try {
		const response = await fetch(
			`${API_BASE}/api/v1/beatgrid-flags/${encodeURIComponent(stableId)}/audio-fit`
		);
		if (!response.ok) throw new Error(`HTTP ${response.status} ${await response.text()}`);
		_entries[stableId] = { state: 'done', fit: (await response.json()) as GridAudioFit };
	} catch (error: unknown) {
		_entries[stableId] = { state: 'failed' };
		console.error(`[grid-audio-fit] could not judge ${stableId}`, error);
	} finally {
		_waiters.delete(stableId);
		_timers.delete(stableId);
	}
}

function release(stableId: string): void {
	if (_entries[stableId]?.state !== 'pending') {
		_waiters.delete(stableId);
		return;
	}
	const left = (_waiters.get(stableId) ?? 1) - 1;
	if (left > 0) {
		_waiters.set(stableId, left);
		return;
	}
	_waiters.delete(stableId);
	const timer = _timers.get(stableId);
	if (timer !== undefined) {
		clearTimeout(timer);
		_timers.delete(stableId);
	}
	delete _entries[stableId];
}

/**
 * Ask the engine about this track after `REQUEST_DELAY_MS`. One ask per
 * successful verdict per page session; a `failed` entry is asked again.
 * Returns a cancel that drops this waiter's claim; the last waiter before
 * the delay elapses prevents the fetch.
 */
export function requestGridAudioFit(stableId: string): () => void {
	const existing = _entries[stableId];
	if (existing?.state === 'done') return () => {};
	if (existing?.state === 'pending') {
		_waiters.set(stableId, (_waiters.get(stableId) ?? 1) + 1);
		return () => release(stableId);
	}
	_entries[stableId] = { state: 'pending' };
	_waiters.set(stableId, 1);
	const timer = setTimeout(() => {
		_timers.delete(stableId);
		if (_entries[stableId]?.state !== 'pending') return;
		void _request(stableId);
	}, REQUEST_DELAY_MS);
	_timers.set(stableId, timer);
	return () => release(stableId);
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
export function deckGridWarningText(
	shapeWarning: string | null,
	fit: GridAudioFit | null
): string | null {
	const audio = fit !== null && fit.deck_warning && fit.message ? fit.message : null;
	if (audio === null) return shapeWarning;
	if (shapeWarning === null) return audio;
	return `${audio} | ${shapeWarning}`;
}
