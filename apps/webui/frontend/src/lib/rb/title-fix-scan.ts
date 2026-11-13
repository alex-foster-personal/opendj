/**
 * TITLEFIXSELECT-12587: start a title-fix scan from the library browser.
 *
 * Selected rows (track menu) or a whole playlist (tree menu) reach the existing
 * `POST /api/v1/title-fix/scans` route with an explicit `stable_ids` scope, the
 * same contract `python -m apps.webui.title_fix_cli scan --ids ...` drives.
 * Proposals are reviewed on /admin?tab=title-fix. A refused id (404) or any other
 * server refusal is toasted verbatim; nothing is retried or substituted.
 */
import { api, unwrap } from '$lib/api/rb-http';
import type { components } from '$lib/api-types';
import { pushToast } from '$lib/stores.svelte';

type TitleFixScan = components['schemas']['ScanOut'];
type ToastFn = (message: string, kind: 'info' | 'warn' | 'error') => void;

/** Mirrors `svc.CFG.MAX_SELECTION` on the server (ScanIn.stable_ids max_length). */
export const TITLE_FIX_MAX_IDS = 1000;
/** The playlist tracks route's largest page. */
const PLAYLIST_PAGE = 500;

export interface TitleFixScanDeps {
	postScan: (stableIds: string[]) => Promise<TitleFixScan>;
	pushToast: ToastFn;
}

export interface PlaylistScanDeps extends TitleFixScanDeps {
	fetchPlaylistPage: (
		playlistId: string,
		offset: number,
		limit: number
	) => Promise<{ tracks: { stable_id: string }[]; next_offset?: number | null }>;
}

/** POST the explicit stable-id scope to the scans route. */
function postScan(stableIds: string[]): Promise<TitleFixScan> {
	return unwrap(api.POST('/api/v1/title-fix/scans', { body: { stable_ids: stableIds } }));
}

/** Read one page of a playlist's membership. */
function fetchPlaylistPage(playlistId: string, offset: number, limit: number) {
	return unwrap(
		api.GET('/api/v1/playlists/{playlist_id}/tracks', {
			params: { path: { playlist_id: playlistId }, query: { offset, limit } }
		})
	);
}

const DEFAULT_DEPS: PlaylistScanDeps = { postScan, pushToast, fetchPlaylistPage };

/** The deduplicated, non-empty scope, or the reason the scan cannot start. */
export function titleFixScanScope(ids: readonly string[]): { ids: string[] } | { refused: string } {
	const unique = [...new Set(ids.filter((id) => id !== ''))];
	if (unique.length === 0)
		return { refused: 'Title-fix scan needs at least one track - select rows first' };
	if (unique.length > TITLE_FIX_MAX_IDS) {
		return {
			refused: `Title-fix scan takes at most ${TITLE_FIX_MAX_IDS} tracks; ${unique.length} selected`
		};
	}
	return { ids: unique };
}

/** Start a scan over the given rows; returns the scan, or null when refused (toasted). */
export async function startTitleFixScanForIds(
	ids: readonly string[],
	deps: TitleFixScanDeps = DEFAULT_DEPS
): Promise<TitleFixScan | null> {
	const scope = titleFixScanScope(ids);
	if ('refused' in scope) {
		deps.pushToast(scope.refused, 'warn');
		return null;
	}
	try {
		const scan = await deps.postScan(scope.ids);
		const n = scope.ids.length;
		deps.pushToast(
			`Title-fix scan started for ${n} track${n === 1 ? '' : 's'} - review in Admin > Title fixes`,
			'info'
		);
		return scan;
	} catch (error) {
		deps.pushToast(error instanceof Error ? error.message : String(error), 'error');
		return null;
	}
}

/** Start a scan over every track in a playlist, in membership order. */
export async function startTitleFixScanForPlaylist(
	playlistId: string,
	deps: PlaylistScanDeps = DEFAULT_DEPS
): Promise<TitleFixScan | null> {
	const ids: string[] = [];
	try {
		let offset: number | null = 0;
		while (offset !== null && ids.length <= TITLE_FIX_MAX_IDS) {
			const page = await deps.fetchPlaylistPage(playlistId, offset, PLAYLIST_PAGE);
			for (const t of page.tracks) ids.push(t.stable_id);
			offset = page.next_offset ?? null;
		}
	} catch (error) {
		deps.pushToast(error instanceof Error ? error.message : String(error), 'error');
		return null;
	}
	if (ids.length === 0) {
		deps.pushToast('Title-fix scan needs at least one track - this playlist is empty', 'warn');
		return null;
	}
	return startTitleFixScanForIds(ids, deps);
}
