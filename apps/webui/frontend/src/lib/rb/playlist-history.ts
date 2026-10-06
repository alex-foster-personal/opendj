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
