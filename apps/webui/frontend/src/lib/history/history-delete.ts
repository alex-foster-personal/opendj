/**
 * Delete logged plays over HTTP (PLAYS-10523). Pure: no Svelte, no DOM.
 */
import { api, API_BASE, unwrap } from '$lib/api/client';
import type { components } from '$lib/api-types';
import { apiErrorFrom } from '$lib/api/errors';
import { refuseStickWrite } from '$lib/rb/track-source';

export type PlaysAfterDelete = components['schemas']['TrackPlaysOut'];

export interface HistoryDayDeleteResult {
	date: string;
	start: string;
	end: string;
	removed: number;
	stable_ids: string[];
}

async function readJson<T>(response: Response): Promise<T> {
	if (!response.ok) {
		throw await apiErrorFrom(response);
	}
	const text = await response.text();
	return JSON.parse(text) as T;
}

export async function deleteHistoryPlay(
	stableId: string,
	playId: string,
	fetchImpl: typeof fetch = fetch
): Promise<PlaysAfterDelete> {
	refuseStickWrite(stableId, 'delete play history');
	return unwrap(
		api.DELETE('/api/v1/tracks/{stable_id}/plays/{play_id}', {
			params: { path: { stable_id: stableId, play_id: playId } },
			fetch: fetchImpl
		})
	);
}

export async function deleteHistoryDay(
	day: string,
	fetchImpl: typeof fetch = fetch,
	tz?: string
): Promise<HistoryDayDeleteResult> {
	const query = tz !== undefined && tz !== '' ? `?tz=${encodeURIComponent(tz)}` : '';
	const response = await fetchImpl(`${API_BASE}/api/v1/history/${day}${query}`, {
		method: 'DELETE',
		headers: { Accept: 'application/json' }
	});
	return readJson<HistoryDayDeleteResult>(response);
}

export interface ConfirmDeleteDeps {
	ask: (cfg: {
		title: string;
		message: string;
		primaryLabel: string;
		secondaryLabel: string;
	}) => Promise<{ ok: boolean }>;
	remove: () => Promise<unknown>;
	refresh: () => void | Promise<void>;
	toast: (msg: string, level: 'info' | 'error') => void;
}

/** Confirm, then delete, then refresh. Returns false when the user cancelled. */
export async function confirmAndDeletePlay(
	deps: ConfirmDeleteDeps,
	what: { title: string; message: string; done: string }
): Promise<boolean> {
	const choice = await deps.ask({
		title: what.title,
		message: what.message,
		primaryLabel: 'Delete',
		secondaryLabel: 'Cancel'
	});
	if (!choice.ok) return false;
	try {
		await deps.remove();
		await deps.refresh();
		deps.toast(what.done, 'info');
		return true;
	} catch (error) {
		deps.toast(`delete failed: ${String(error)}`, 'error');
		return false;
	}
}
