/**
 * Reserved Autolists pane sentinel (issue #2066).
 */
import type { PlaylistNode } from '$lib/rb/library-types';

export const AUTOLIST_ID = 'autolist';

export function isAutolistId(id: string | null): boolean {
	return id === AUTOLIST_ID;
}

export function autolistNode(title: string, trackCount: number): PlaylistNode {
	return {
		playlist_id: AUTOLIST_ID,
		name: title,
		track_count: trackCount,
		broken_count: 0,
		kind: 'autolist',
		children: []
	};
}
