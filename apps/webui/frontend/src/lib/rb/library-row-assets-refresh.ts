/**
 * When stems or derived vocals land, refresh visible library row assets and
 * any loaded deck holding those tracks, without reloading the track (#11422).
 */
import { subscribeKind, subscribeResync } from '$lib/api/events-bus';
import type { RowAssetTarget } from '$lib/rb/row-assets-fill';
import {
	isStableIdOnLoadedDeck,
	refreshLoadedDeckVocalsFromServer
} from '$lib/rb/loaded-deck-vocals-refresh';
import { requestLibraryRowAssetsRefresh } from '$lib/rb/library-row-assets-refresh-request';

/** Rows whose settled row-assets should be dropped and re-asked. */
export function rowAssetRefreshTargets(
	ids: readonly string[],
	rows: readonly RowAssetTarget[]
): string[] {
	const wanted = ids.length === 0 ? new Set(rows.map((row) => row.stable_id)) : new Set(ids);
	const out: string[] = [];
	for (const row of rows) {
		if (wanted.has(row.stable_id)) out.push(row.stable_id);
	}
	return out;
}

export function applyRowAssetsRefresh(
	ids: readonly string[],
	rows: readonly RowAssetTarget[],
	settled: WeakSet<object>,
	nudge: (stable_ids: readonly string[]) => void
): string[] {
	const targets = rowAssetRefreshTargets(ids, rows);
	if (targets.length === 0) return [];
	for (const row of rows) {
		if (targets.includes(row.stable_id)) settled.delete(row);
	}
	nudge(targets);
	return targets;
}

function _refreshForEventIds(ids: string[]): void {
	if (ids.length === 0) return;
	requestLibraryRowAssetsRefresh(ids);
	const onDeck = ids.filter((stable_id) => isStableIdOnLoadedDeck(stable_id));
	if (onDeck.length > 0) void refreshLoadedDeckVocalsFromServer(onDeck);
}

/** Events-bus hook for BrowserPanel onMount cleanup. */
export function subscribeStemsAndVocalsLibraryRefresh(): () => void {
	const unsubscribeTracks = subscribeKind('tracks', _refreshForEventIds);
	const unsubscribeJobs = subscribeKind('library_jobs', _refreshForEventIds);
	const unsubscribeResync = subscribeResync(() => requestLibraryRowAssetsRefresh([]));
	return () => {
		unsubscribeTracks();
		unsubscribeJobs();
		unsubscribeResync();
	};
}
