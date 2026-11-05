/**
 * LIBUX-8676: one undo / redo / history jump, with its toast, and an optional
 * optimistic restore on screen while the write is in flight.
 *
 * the maintainer, Wed 4 Nov 2026: "At minimum pressing undo should show a toast.
 * Actually - show toast for actions like adding tracks, deleting them,
 * undoing something, just the important CRUDs a user needs to know straight
 * away as they'll be precious about their tracks a bit."
 *
 * Every undo path (button, Cmd+Z, toast action, agent bus `playlist_undo`)
 * reaches the history adapter, so the toast is raised here once, not at each
 * caller. Pure: the transport, the restorer and the toast sink are injected.
 */

import type {
	PlaylistHistoryApplyResult,
	PlaylistHistoryEntry,
	PlaylistHistoryJumpResult
} from './playlist-history';

/** Undo/redo toasts share one group, so a burst of Cmd+Z is ONE toast (xN). */
export const HISTORY_TOAST_GROUP = 'playlist-history';
/** Row add/remove confirmations share one group for the same reason. */
export const TRACK_CRUD_TOAST_GROUP = 'playlist-track-crud';

export interface HistoryToastAction {
	label: string;
	handler: () => void;
}

export type HistoryToastSink = (message: string, action?: HistoryToastAction) => void;

/** A restore already on screen (see pane-row-removal.ts restoreRowsInPlace). */
export interface PendingHistoryRestore {
	commit(etag: string | undefined): void;
	revert(): void;
}

/** Offered the entry an undo is about to reverse; null = no in-place restore. */
export type HistoryUndoRestorer = (entry: PlaylistHistoryEntry) => PendingHistoryRestore | null;

/** "Undone: Remove 'Song' from 'INBOX'"; the op when the label is unknown. */
export function historyStepToastMessage(action: 'undo' | 'redo', label: string): string {
	return `${action === 'undo' ? 'Undone' : 'Redone'}: ${label}`;
}

/** A history jump: "Undid 3 edits", "Redid 1 edit". */
export function historyJumpToastMessage(result: Pick<PlaylistHistoryJumpResult, 'steps'>): string | null {
	const steps = result.steps;
	if (steps.length === 0) return null;
	const verb = steps.every((step) => step.action === 'undo') ? 'Undid' : 'Redid';
	return `${verb} ${steps.length} ${steps.length === 1 ? 'edit' : 'edits'}`;
}

/** "Removed 'Track 2' from 'INBOX'" or "Removed 3 tracks from 'INBOX'". */
export function removedTracksToastMessage(titles: readonly string[], playlistName: string | null): string {
	const what = titles.length === 1 ? `'${titles[0]}'` : `${titles.length} tracks`;
	return playlistName === null ? `Removed ${what} from playlist` : `Removed ${what} from '${playlistName}'`;
}

/**
 * Send one undo or redo. `expected` is the entry the step should reverse
 * (the one under / at the cursor in the last history read). An undo offers
 * it to `restorer` first, so restored rows are on screen before the write
 * returns; a failed write, or one that reversed a DIFFERENT entry (the read
 * was stale), takes them back out. A success always toasts, with the
 * opposite step as its action.
 */
export async function applyHistoryStep(opts: {
	action: 'undo' | 'redo';
	expected: PlaylistHistoryEntry | undefined;
	send: () => Promise<PlaylistHistoryApplyResult>;
	restorer: HistoryUndoRestorer | null;
	toast: HistoryToastSink;
	opposite: () => void;
}): Promise<PlaylistHistoryApplyResult> {
	const { action, expected, send, restorer, toast, opposite } = opts;
	const pending = action === 'undo' && expected !== undefined && restorer !== null ? restorer(expected) : null;
	let result: PlaylistHistoryApplyResult;
	try {
		result = await send();
	} catch (error) {
		pending?.revert();
		throw error;
	}
	const matched = expected !== undefined && expected.command_id === result.command_id;
	if (pending !== null) {
		if (matched) pending.commit(result.etag);
		else pending.revert();
	}
	const label = matched ? expected.label : result.op;
	toast(historyStepToastMessage(action, label), {
		label: action === 'undo' ? 'Redo' : 'Undo',
		handler: opposite
	});
	return result;
}
