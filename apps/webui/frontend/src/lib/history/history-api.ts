/**
 * Play History over HTTP (PLAYS-9745), rekordbox History parity.
 *
 * Hand-written and typed to the shapes of `apps/webui/server/routes/play_history.py`
 * (`HistoryDaysOut`, `HistoryDayPlaysOut`, `HistoryPlaylistOut`). The routes land in
 * PR #14344, which is not in this branch's base, so the generated `api-types.ts`
 * does not carry them yet. Pure: no Svelte, no DOM.
 */
import { API_BASE } from '$lib/api/base';
import { apiErrorFrom } from '$lib/api/errors';

export interface HistoryDay {
	/** YYYY-MM-DD: the night that starts at 06:00 local on this date. */
	date: string;
	count: number;
}

export interface HistoryDays {
	rollover_hour: number;
	days: HistoryDay[];
}

export interface HistoryPlay {
	position: number;
	stable_id: string;
	play_id: string | null;
	played_at: string;
	deck: number | null;
	source: string;
}

export interface HistoryDayPlays {
	date: string;
	start: string;
	end: string;
	plays: HistoryPlay[];
}

export interface HistoryPlaylistSaved {
	playlist_id: string;
	name: string;
	track_count: number;
	skipped: string[];
}

function tzQuery(tz: string | undefined): string {
	return tz !== undefined && tz !== '' ? `?tz=${encodeURIComponent(tz)}` : '';
}

async function readJson<T>(response: Response): Promise<T> {
	if (!response.ok) throw await apiErrorFrom(response);
	return (await response.json()) as T;
}

/** GET /history: the nights that hold plays, newest first, with counts. */
export async function fetchHistoryDays(
	fetchImpl: typeof fetch = fetch,
	tz?: string
): Promise<HistoryDays> {
	const response = await fetchImpl(`${API_BASE}/api/v1/history${tzQuery(tz)}`, {
		headers: { Accept: 'application/json' }
	});
	return readJson<HistoryDays>(response);
}

/** GET /history/{day}: that night's plays in play order. */
export async function fetchHistoryDay(
	day: string,
	fetchImpl: typeof fetch = fetch,
	tz?: string
): Promise<HistoryDayPlays> {
	const response = await fetchImpl(
		`${API_BASE}/api/v1/history/${encodeURIComponent(day)}${tzQuery(tz)}`,
		{
			headers: { Accept: 'application/json' }
		}
	);
	return readJson<HistoryDayPlays>(response);
}

/** POST /history/{day}/playlist: a normal playlist of that night's tracks in play order. */
export async function saveHistoryDayAsPlaylist(
	day: string,
	name: string | null = null,
	fetchImpl: typeof fetch = fetch,
	tz?: string
): Promise<HistoryPlaylistSaved> {
	const response = await fetchImpl(
		`${API_BASE}/api/v1/history/${encodeURIComponent(day)}/playlist${tzQuery(tz)}`,
		{
			method: 'POST',
			headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
			body: JSON.stringify(name === null ? {} : { name })
		}
	);
	return readJson<HistoryPlaylistSaved>(response);
}
