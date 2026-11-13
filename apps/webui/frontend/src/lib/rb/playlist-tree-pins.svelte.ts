/**
 * LIBM-87 (issue #12320): pinned playlists, the tree's shortcuts.
 *
 * The pins live on the server (`/api/v1/playlist-tree/pins`), in their own
 * overlay apart from the tree order, so pinning never moves a playlist. The
 * tree reads them here and follows `library.changed` kind `playlists`, which
 * the pin routes publish, so a pin set by an agent over HTTP shows the same
 * way as one set from the tree's pin button.
 */
import { subscribeKind, subscribeResync } from '$lib/api/events-bus';
import { isPageGoingAway } from '$lib/api/lifecycle-abort';
import { api, unwrap } from '$lib/api/rb-http';
import { pushToast } from '$lib/stores.svelte';

export const playlistTreePins = $state<{ ids: string[] }>({ ids: [] });

let started = false;

/** Read the stored pins (live playlists only, in pin order). */
export async function refreshPlaylistTreePins(): Promise<void> {
	playlistTreePins.ids = (await unwrap(api.GET('/api/v1/playlist-tree/pins', {}))).pins;
}

/** Pin or unpin one playlist; the tree shows the server's answer. */
export async function setPlaylistPinned(playlistId: string, pinned: boolean): Promise<void> {
	const params = { params: { path: { playlist_id: playlistId } } };
	const out = pinned
		? await unwrap(api.PUT('/api/v1/playlist-tree/pins/{playlist_id}', params))
		: await unwrap(api.DELETE('/api/v1/playlist-tree/pins/{playlist_id}', params));
	playlistTreePins.ids = out.pins;
}

function _refreshLoudly(): void {
	refreshPlaylistTreePins().catch((exc: unknown) => {
		if (isPageGoingAway()) return;
		pushToast(`pinned playlists did not load: ${String(exc)}`, 'error');
	});
}

/** Load the pins once for the page and keep following changes. Safe to call repeatedly. */
export function ensurePlaylistTreePinsLoaded(): void {
	if (started) return;
	started = true;
	subscribeKind('playlists', _refreshLoudly);
	subscribeResync(_refreshLoudly);
	_refreshLoudly();
}

/** The playlists the pins name, in pin order, found at any folder depth; unknown ids and non-playlist nodes are skipped. */
export function pinnedShortcutNodes<T extends { playlist_id: string; kind: string; children: T[] }>(
	nodes: readonly T[],
	pins: readonly string[]
): T[] {
	const byId = new Map<string, T>();
	const walk = (list: readonly T[]): void => {
		for (const node of list) {
			byId.set(node.playlist_id, node);
			walk(node.children);
		}
	};
	walk(nodes);
	return pins.flatMap((id) => {
		const node = byId.get(id);
		return node === undefined || node.kind !== 'playlist' ? [] : [node];
	});
}
