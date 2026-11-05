/**
 * Playlist edit undo/redo client. Standalone from api-rb.ts (hotspot) and
 * from playlist-write.ts (write mutations). Transport only: GET the live
 * inverse-command window, POST undo / redo. Cmd+Z and the history panel
 * dispatch through performance-ipc so UI and agents share one path.
 */

import { ApiError, api, unwrap } from '../api/client';
import { PlaylistConflictError, type PlaylistRowWire } from './playlist-write';

export { PlaylistConflictError };

export class PlaylistHistoryEmptyError extends Error {
	constructor(public readonly code: 'nothing_to_undo' | 'nothing_to_redo') {
		super(code);
		this.name = 'PlaylistHistoryEmptyError';
	}
}

export interface PlaylistHistoryEntry {
	command_id: string;
	op: string;
	playlist_id: string;
	ts: string;
	label: string;
	/** Tracks the edit added or removed (LIBUX-8626 navigation); empty for
	 * header-only edits and reorders. Optional only for pre-field servers. */
	stable_ids?: string[];
}

export interface PlaylistHistory {
	cursor: number;
	limit: number;
	can_undo: boolean;
	can_redo: boolean;
	entries: PlaylistHistoryEntry[];
}

/**
 * What the undo button will do (LIBUX-51), for its title and the head of its
 * hover card: names the edit the next undo reverses, which is the entry just
 * below the cursor. With nothing to undo it says so. If the server reports an undo but the window lacks its entry, the
 * title falls back to the generic wording instead of naming the wrong edit.
 */
export function undoButtonTitle(entries: readonly PlaylistHistoryEntry[], cursor: number, canUndo: boolean): string {
	if (!canUndo) return 'Nothing to undo';
	const top = cursor > 0 ? entries[cursor - 1] : undefined;
	if (top === undefined) return 'Undo playlist edit (Ctrl/Cmd+Z)';
	return `Undo: ${top.label} (Ctrl/Cmd+Z)`;
}

// ---------------------------------------------------------------- popover
// LIBUX-8626 (the maintainer, Wed 4 Nov 2026): the undo button's hover list is the full
// history, newest first, and stays open so it can be scrolled and clicked.

/** Hover intent before the history popover opens. */
export const HISTORY_HOVER_OPEN_MS = 1000;
/** Time to cross the gap from button to popover before it closes. */
export const HISTORY_CLOSE_GRACE_MS = 250;
/** Settle time before hovering a row moves the library to what it touched. */
export const HISTORY_NAV_HOVER_MS = 300;

export interface PlaylistHistoryRow {
	entry: PlaylistHistoryEntry;
	/** True when the edit is live (below the cursor); false when undone. */
	applied: boolean;
	/** True for the edit the next plain undo reverses. */
	next: boolean;
	/** What clicking the row does, for its title. */
	action: string;
}

/** The whole window newest first, each row saying what a click on it does. */
export function historyRowsNewestFirst(
	entries: readonly PlaylistHistoryEntry[],
	cursor: number
): PlaylistHistoryRow[] {
	return entries
		.map((entry, index) => {
			const applied = index < cursor;
			const others = applied ? cursor - 1 - index : index - cursor;
			const action = applied
				? others === 0
					? 'Click to undo this edit'
					: `Click to undo this edit and ${others} newer`
				: others === 0
					? 'Click to redo this edit'
					: `Click to redo this edit and ${others} older`;
			return { entry, applied, next: index === cursor - 1, action };
		})
		.reverse();
}

export interface PlaylistHistoryNavTarget {
	playlist_id: string;
	stable_id?: string;
}

/**
 * Where hovering a row should take the library: the playlist it edited, and
 * its first track when it added or removed tracks. Null when that playlist
 * does not exist right now (an undone create, a live delete).
 */
export function historyEntryNavTarget(row: PlaylistHistoryRow): PlaylistHistoryNavTarget | null {
	const { entry, applied } = row;
	const created = entry.op === 'create' || entry.op === 'duplicate';
	if (created && !applied) return null;
	if (entry.op === 'delete' && applied) return null;
	const stableId = entry.stable_ids?.[0];
	return stableId === undefined ? { playlist_id: entry.playlist_id } : { playlist_id: entry.playlist_id, stable_id: stableId };
}

export interface PlaylistHistoryJumpResult {
	cursor: number;
	can_undo: boolean;
	can_redo: boolean;
	steps: { command_id: string; op: string; action: 'undo' | 'redo'; playlist_id: string }[];
}

export interface PlaylistHistoryApplyResult {
	command_id: string;
	op: string;
	action: 'undo' | 'redo';
	playlist_id: string;
	current: PlaylistRowWire | null;
	etag?: string;
	can_undo: boolean;
	can_redo: boolean;
}

function _messageOf(error: ApiError): string {
	return (error.body as { message?: string } | null)?.message ?? error.message;
}

function _throwHistoryError(error: unknown, describe: string): never {
	if (error instanceof ApiError && error.status === 409) {
		const body = error.body as {
			error?: string;
			current?: PlaylistRowWire;
			etag?: string;
		} | null;
		if (body?.error === 'nothing_to_undo' || body?.error === 'nothing_to_redo') {
			throw new PlaylistHistoryEmptyError(body.error);
		}
		if (body?.error === 'conflict' && body.current !== undefined && body.etag) {
			throw new PlaylistConflictError(body.current, body.etag);
		}
	}
	if (error instanceof ApiError) {
		throw new Error(`${describe} failed (${error.status}): ${_messageOf(error)}`);
	}
	throw error;
}

export async function fetchPlaylistHistory(): Promise<PlaylistHistory> {
	try {
		return await unwrap(api.GET('/api/v1/playlist-history'));
	} catch (error) {
		if (error instanceof ApiError) {
			throw new Error(`fetch playlist history failed (${error.status}): ${_messageOf(error)}`);
		}
		throw error;
	}
}

export async function undoPlaylistEdit(): Promise<PlaylistHistoryApplyResult> {
	try {
		const data = await unwrap(api.POST('/api/v1/playlist-history/undo'));
		if (data === undefined || data === null) {
			throw new Error('undo playlist edit failed: response has no body');
		}
		return data as PlaylistHistoryApplyResult;
	} catch (error) {
		_throwHistoryError(error, 'undo playlist edit');
	}
}

export async function redoPlaylistEdit(): Promise<PlaylistHistoryApplyResult> {
	try {
		const data = await unwrap(api.POST('/api/v1/playlist-history/redo'));
		if (data === undefined || data === null) {
			throw new Error('redo playlist edit failed: response has no body');
		}
		return data as PlaylistHistoryApplyResult;
	} catch (error) {
		_throwHistoryError(error, 'redo playlist edit');
	}
}

/** POST /playlist-history/jump: undo an applied entry and every newer one,
 * or redo a pending entry and every older one. */
export async function jumpPlaylistHistory(commandId: string): Promise<PlaylistHistoryJumpResult> {
	try {
		const data = await unwrap(
			api.POST('/api/v1/playlist-history/jump', { body: { command_id: commandId } })
		);
		if (data === undefined || data === null) {
			throw new Error('jump playlist history failed: response has no body');
		}
		return data as PlaylistHistoryJumpResult;
	} catch (error) {
		_throwHistoryError(error, 'jump playlist history');
	}
}
