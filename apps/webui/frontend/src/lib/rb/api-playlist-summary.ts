import { API_BASE } from '$lib/api';

export interface PlaylistSummaryStatsDoc {
	tracks: number;
	duration_s: number;
	bpm: { min: number | null; median: number | null; max: number | null; missing: number };
	energy: { min: number | null; max: number | null; missing: number };
	keys: { distinct: number; missing: number };
	unanalysed: number;
}

export async function fetchPlaylistSummary(playlistId: string): Promise<PlaylistSummaryStatsDoc> {
	const url = `${API_BASE}/api/v1/playlists/${encodeURIComponent(playlistId)}/summary`;
	const response = await fetch(url);
	if (!response.ok) {
		throw new Error(`GET playlist summary ${playlistId} failed (${response.status})`);
	}
	return (await response.json()) as PlaylistSummaryStatsDoc;
}
