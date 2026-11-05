/** HTTP client for the set target routes (LIBUX-8679). Same documents as `opendj set` and MCP set_*. */

import { API_BASE } from '$lib/api';
import type { KeyClash } from './set-target';

export interface SetSummaryDoc {
	playlist_id: string;
	played_s: number;
	target_s: number | null;
	over_s: number | null;
	skipped: number;
	skipped_ids: string[];
	clashes: KeyClash[];
	readout: string;
}

const base = (playlistId: string): string => `${API_BASE}/api/v1/playlists/${encodeURIComponent(playlistId)}`;

async function send(method: 'GET' | 'PUT', path: string, body?: unknown): Promise<SetSummaryDoc> {
	const init: RequestInit =
		body === undefined
			? { method }
			: { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
	const response = await fetch(path, init);
	if (!response.ok) throw new Error(`${method} ${path} returned ${response.status}: ${await response.text()}`);
	return (await response.json()) as SetSummaryDoc;
}

export const fetchSetSummary = (playlistId: string): Promise<SetSummaryDoc> =>
	send('GET', `${base(playlistId)}/set-summary`);

export const putSetTarget = (playlistId: string, targetS: number | null): Promise<SetSummaryDoc> =>
	send('PUT', `${base(playlistId)}/set-target`, { target_s: targetS });

export const putSetSkip = (playlistId: string, stableId: string, skipped: boolean): Promise<SetSummaryDoc> =>
	send('PUT', `${base(playlistId)}/set-skips/${encodeURIComponent(stableId)}`, { skipped });
