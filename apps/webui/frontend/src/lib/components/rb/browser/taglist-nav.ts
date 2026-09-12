import type { PlaylistNode } from '$lib/rb/library-types';

export const TAGLIST_ID_PREFIX = 'taglist:';

export function taglistPaneId(tagName: string): string {
	return `${TAGLIST_ID_PREFIX}${tagName}`;
}

export function tagNameFromPaneId(playlistId: string): string | null {
	if (!playlistId.startsWith(TAGLIST_ID_PREFIX)) return null;
	const name = playlistId.slice(TAGLIST_ID_PREFIX.length);
	return name === '' ? null : name;
}

export function tracksQueryForTaglist(playlistId: string): { tag: string } {
	const name = tagNameFromPaneId(playlistId);
	if (name === null) {
		throw new Error(`tracksQueryForTaglist: not a taglist pane id: ${playlistId}`);
	}
	return { tag: name };
}

export function taglistNode(tag: { name: string; track_count: number }): PlaylistNode {
	return {
		playlist_id: taglistPaneId(tag.name),
		name: tag.name,
		track_count: tag.track_count,
		broken_count: 0,
		kind: 'taglist',
		children: []
	};
}

export function filterRowsByTag<T extends { tags?: readonly string[] | null }>(
	rows: readonly T[],
	tag: string
): T[] {
	return rows.filter((row) => row.tags?.includes(tag) ?? false);
}
