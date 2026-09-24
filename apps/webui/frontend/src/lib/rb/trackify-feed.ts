/**
 * Trackify playlist feed hydrator (PLAY-04 snapshot, no BrowserPanel import).
 */
import type { AutoPlayTrackRow } from '$lib/rb/auto-play-chain';
import { createAutoPlayFeedSnapshot } from '$lib/rb/autoplay-feed';
import { listPlaylistTracksPage, listTracksHydrated, type PlaylistTrackRowWire, type TrackListItemWire } from '$lib/rb/api-rb';
import type { LastPlaylistPref } from '$lib/rb/prefs-types';

const PAGE_SIZE = 500;
const MAX_PAGES = 200;

export const TRACKIFY_ALL_TRACKS_SCOPE = 'all_tracks';

export function trackifyPlaylistScope(lastPlaylist: LastPlaylistPref | null): string {
	if (lastPlaylist === null || lastPlaylist.kind === 'all_tracks') return TRACKIFY_ALL_TRACKS_SCOPE;
	return `playlist:${lastPlaylist.playlist_id}`;
}

function _rowFromTrack(item: TrackListItemWire | PlaylistTrackRowWire): AutoPlayTrackRow {
	return {
		stable_id: item.stable_id,
		key: item.key ?? null,
		bpm: item.bpm ?? null,
		file_exists: item.file_exists === true,
		title: item.title ?? null,
		artist: item.artist ?? null
	};
}

async function _fetchAllTracksRows(): Promise<AutoPlayTrackRow[]> {
	const rows: AutoPlayTrackRow[] = [];
	let cursor: string | undefined;
	for (let page = 0; page < MAX_PAGES; page += 1) {
		const pageResult = await listTracksHydrated({ limit: PAGE_SIZE, cursor });
		for (const item of pageResult.items) rows.push(_rowFromTrack(item));
		if (pageResult.next_cursor === null || pageResult.next_cursor === '') return rows;
		cursor = pageResult.next_cursor;
	}
	// A live continuation past MAX_PAGES * PAGE_SIZE rows would otherwise be
	// presented as a complete feed while playable tracks are silently
	// omitted -- fail loud instead (Sol review, PR #3676).
	throw new Error(
		`Trackify: all-tracks feed exceeds ${MAX_PAGES * PAGE_SIZE} rows with more still available; refusing an incomplete feed`
	);
}

async function _fetchPlaylistRows(playlistId: string): Promise<AutoPlayTrackRow[]> {
	const rows: AutoPlayTrackRow[] = [];
	let offset = 0;
	for (let page = 0; page < MAX_PAGES; page += 1) {
		const { page: slice } = await listPlaylistTracksPage(playlistId, {
			limit: PAGE_SIZE,
			offset
		});
		for (const item of slice.tracks) rows.push(_rowFromTrack(item));
		if (slice.next_offset === null) return rows;
		offset = slice.next_offset;
	}
	throw new Error(
		`Trackify: playlist ${playlistId} feed exceeds ${MAX_PAGES * PAGE_SIZE} rows with more still available; refusing an incomplete feed`
	);
}

export async function fetchTrackifyViewRows(lastPlaylist: LastPlaylistPref | null): Promise<AutoPlayTrackRow[]> {
	if (lastPlaylist === null || lastPlaylist.kind === 'all_tracks') return _fetchAllTracksRows();
	return _fetchPlaylistRows(lastPlaylist.playlist_id);
}

export interface TrackifyFeedSnapshot {
	readonly scope: string;
	readonly epoch: number;
	readonly rows: readonly AutoPlayTrackRow[];
	readonly snapshotted: boolean;
}

export function createTrackifyFeedController(): {
	step(enabled: boolean, lastPlaylist: LastPlaylistPref | null, viewRows: readonly AutoPlayTrackRow[]): TrackifyFeedSnapshot;
	readonly epoch: number;
	readonly rows: readonly AutoPlayTrackRow[];
} {
	const feedSnapshot = createAutoPlayFeedSnapshot();
	let epoch = 0;
	let rows: AutoPlayTrackRow[] = [];
	let scope = '';
	return {
		get epoch() {
			return epoch;
		},
		get rows() {
			return rows;
		},
		step(enabled, lastPlaylist, viewRows) {
			const nextScope = trackifyPlaylistScope(lastPlaylist);
			const decision = feedSnapshot.step(enabled, nextScope, viewRows);
			if (decision.publish !== null) {
				rows = decision.publish.slice();
				scope = nextScope;
				if (decision.snapshotted || decision.publish.length > 0) epoch += 1;
			}
			return {
				scope,
				epoch,
				rows,
				snapshotted: decision.snapshotted
			};
		}
	};
}
