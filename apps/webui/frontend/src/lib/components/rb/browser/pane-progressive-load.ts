/**
 * First-page paint + seq-guarded append for All Tracks (issue #318).
 *
 * Lives outside pane-contract.svelte.ts: that file is at the frontend
 * size ratchet and must not grow. No $state, so node:test can load this
 * module directly. Callers pass a PaneStore (or any duck-typed pane).
 *
 * After the first page is on screen, later pages only append. A final
 * completeLoad would rebuild every BrowserRow and wipe revealed / rb_meta
 * on rows the user already saw. finishFill clears load_progress without
 * replacing rows.
 */

export interface ProgressiveLoadPane<Row = unknown> {
	rows: Row[];
	loading: boolean;
	truncated: boolean;
	etag: string;
	load_progress: { loaded: number; total: number | null } | null;
	selected_id: string | null;
	selected_ids: string[];
	scroll_top: number;
	isCurrentLoad(seq: number): boolean;
}

/** Publish the first cursor page and clear the blocking spinner.
 * Leaves load_progress set so the overlay can say "loading more".
 * Does not touch selection or scroll. Stale seq is a full no-op. */
export function publishFirstPage<Row>(
	pane: ProgressiveLoadPane<Row>,
	seq: number,
	rows: Row[]
): boolean {
	if (!pane.isCurrentLoad(seq)) return false;
	pane.rows = rows;
	pane.loading = false;
	pane.truncated = false;
	pane.etag = '';
	return true;
}

/** Append later pages onto the already-painted rows. Empty extras
 * (confirming empty page) and stale seq are no-ops. Does not touch
 * selection, scroll, or loading. */
export function appendRows<Row>(
	pane: ProgressiveLoadPane<Row>,
	seq: number,
	extra: Row[]
): boolean {
	if (!pane.isCurrentLoad(seq)) return false;
	if (extra.length === 0) return false;
	pane.rows = [...pane.rows, ...extra];
	return true;
}

/** Settle a background fill: clear load_progress, keep painted rows.
 * `truncated` is true when the walk failed after first paint. */
export function finishFill(
	pane: ProgressiveLoadPane,
	seq: number,
	truncated = false
): boolean {
	if (!pane.isCurrentLoad(seq)) return false;
	pane.loading = false;
	pane.truncated = truncated;
	pane.load_progress = null;
	return true;
}
