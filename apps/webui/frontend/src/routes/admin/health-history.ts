/**
 * Pure helpers for health snapshot history on the admin Diagnostics tab.
 */

import type { HealthOut } from '$lib/api';

export const HEALTH_HISTORY_LIMIT = 20;
export const HEALTH_STALE_AFTER_MS = 60_000;

export type HealthHistoryEntry = {
	at: number;
	status: string;
	tracks: number;
	playlists: number;
	lockHolder: string | null;
	syncthingPeers: number | null;
	syncthingFolder: string | null;
	bindHost: string;
};

export function entryFromHealth(data: HealthOut, at: number): HealthHistoryEntry {
	return {
		at,
		status: data.status,
		tracks: data.state_db.tracks,
		playlists: data.state_db.playlists,
		lockHolder: data.cloud.lock_holder?.holder ?? null,
		syncthingPeers: data.syncthing?.peers_connected ?? null,
		syncthingFolder: data.syncthing?.folder_state ?? null,
		bindHost: data.bind_host
	};
}

export function pushHistory(
	history: HealthHistoryEntry[],
	entry: HealthHistoryEntry
): HealthHistoryEntry[] {
	const next = [...history, entry];
	if (next.length <= HEALTH_HISTORY_LIMIT) return next;
	return next.slice(next.length - HEALTH_HISTORY_LIMIT);
}

export function staleness(
	lastOkAt: number | null,
	now: number
): { ageMs: number | null; stale: boolean } {
	if (lastOkAt === null) return { ageMs: null, stale: true };
	const ageMs = now - lastOkAt;
	return { ageMs, stale: ageMs > HEALTH_STALE_AFTER_MS };
}
