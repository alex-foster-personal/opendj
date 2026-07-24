/**
 * Blank playlist identity + create-grace registry for auto-delete.
 *
 * Blank = empty membership AND name still a create-default / untitled
 * sentinel (case-insensitive trim). A user-renamed empty playlist must
 * survive until they delete it. Newly created blanks get a short grace
 * window so in-place rename after '+' can finish.
 */

export const DEFAULT_PLAYLIST_NAME = 'New Playlist';
export const BLANK_PLAYLIST_GRACE_MS = 60_000;

const LOG_PREFIX = '[playlist-blank]';

/** Case-folded create-default / untitled sentinels. */
const BLANK_NAME_SENTINELS = new Set([
	'new playlist',
	'untitled',
	'untitled playlist',
	''
]);

/** playlist_id -> grace deadline (epoch ms). */
const _graceUntil = new Map<string, number>();

export type BlankPlaylistLike = {
	playlist_id: string;
	name: string;
	track_count: number;
	/** When set, only `webui` rows are delete candidates. */
	vendor?: string;
};

export type BlankDeleteDecision =
	| { action: 'delete'; reason: string }
	| { action: 'keep'; reason: string };

export function isBlankPlaylistName(name: string): boolean {
	return BLANK_NAME_SENTINELS.has(name.trim().toLowerCase());
}

/** Empty membership + still default/untitled identity. */
export function isBlankPlaylist(p: BlankPlaylistLike): boolean {
	return p.track_count === 0 && isBlankPlaylistName(p.name);
}

export function markPlaylistCreateGrace(playlistId: string, nowMs: number = Date.now()): void {
	_graceUntil.set(playlistId, nowMs + BLANK_PLAYLIST_GRACE_MS);
}

export function clearPlaylistCreateGrace(playlistId: string): void {
	_graceUntil.delete(playlistId);
}

export function isWithinCreateGrace(playlistId: string, nowMs: number = Date.now()): boolean {
	const until = _graceUntil.get(playlistId);
	if (until === undefined) return false;
	if (nowMs >= until) {
		_graceUntil.delete(playlistId);
		return false;
	}
	return true;
}

/** Test-only: clear in-memory grace registry. */
export function _resetCreateGraceForTests(): void {
	_graceUntil.clear();
}

export function decideBlankPlaylistDelete(
	p: BlankPlaylistLike,
	nowMs: number = Date.now()
): BlankDeleteDecision {
	if (p.vendor !== undefined && p.vendor !== 'webui') {
		return { action: 'keep', reason: `vendor=${p.vendor} (not webui)` };
	}
	if (p.track_count > 0) {
		return { action: 'keep', reason: `has ${p.track_count} track(s)` };
	}
	if (!isBlankPlaylistName(p.name)) {
		return { action: 'keep', reason: `renamed name=${JSON.stringify(p.name.trim())}` };
	}
	if (isWithinCreateGrace(p.playlist_id, nowMs)) {
		return { action: 'keep', reason: `within ${BLANK_PLAYLIST_GRACE_MS}ms create grace` };
	}
	return {
		action: 'delete',
		reason: 'blank untitled empty (grace elapsed or absent)'
	};
}

export type BlankSweepLog = (message: string) => void;

/**
 * Decide which empty playlists to auto-delete. Logs every empty-row
 * decision (delete or keep-with-reason). Non-empty rows are never
 * candidates and are not logged.
 */
export function collectBlankPlaylistDeletes(
	playlists: readonly BlankPlaylistLike[],
	nowMs: number = Date.now(),
	log: BlankSweepLog = (msg) => console.info(msg)
): string[] {
	const ids: string[] = [];
	for (const p of playlists) {
		if (p.track_count > 0) continue;
		const d = decideBlankPlaylistDelete(p, nowMs);
		if (d.action === 'delete') {
			log(`${LOG_PREFIX} delete ${p.playlist_id}: ${d.reason}`);
			ids.push(p.playlist_id);
		} else {
			log(`${LOG_PREFIX} keep ${p.playlist_id}: ${d.reason}`);
		}
	}
	return ids;
}
