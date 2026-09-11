/**
 * Playlist drag payload - the dataTransfer codec for a playlist dragged
 * out of the tree (PlaylistTree) onto the tab bar (PaneTabs).
 *
 * Split out of pane-contract.svelte.ts: this is a pure serialization
 * boundary with no pane state, no runes and no imports, so it belongs
 * beside the contract rather than inside it. `PlaylistDragPayload` stays
 * re-exported from pane-contract for existing importers, the same
 * compatibility shape TrackTable already uses for the row vocabulary.
 */

/** dataTransfer type for a playlist dragged out of the tree onto the tabs. */
export const PLAYLIST_DRAG_MIME = 'application/x-mdt-playlist';

/**
 * The subset of PlaylistNode that survives a drag. Children are dropped
 * because the payload crosses a dataTransfer JSON round trip and the tab
 * bar only ever opens the dragged node itself.
 */
export interface PlaylistDragPayload {
	playlist_id: string;
	name: string;
	track_count: number;
	kind: 'all_tracks' | 'playlist' | 'folder';
}

/** Serialize a playlist for dataTransfer. */
export function encodePlaylistDrag(payload: PlaylistDragPayload): string {
	return JSON.stringify(payload);
}

/**
 * Parse a dropped playlist payload, or null when the drop is not one of
 * ours. Returns null rather than throwing because a drop handler receives
 * whatever the OS hands it - foreign drags are an expected input, not a bug.
 */
export function decodePlaylistDrag(raw: string): PlaylistDragPayload | null {
	if (raw.trim() === '') return null;
	let parsed: unknown;
	try {
		parsed = JSON.parse(raw);
	} catch {
		return null;
	}
	if (typeof parsed !== 'object' || parsed === null) return null;
	const record = parsed as Record<string, unknown>;
	const { playlist_id, name, track_count, kind } = record;
	if (typeof playlist_id !== 'string' || playlist_id === '') return null;
	if (typeof name !== 'string') return null;
	if (typeof track_count !== 'number' || !Number.isFinite(track_count)) return null;
	if (kind !== 'all_tracks' && kind !== 'playlist' && kind !== 'folder') return null;
	return { playlist_id, name, track_count, kind };
}
