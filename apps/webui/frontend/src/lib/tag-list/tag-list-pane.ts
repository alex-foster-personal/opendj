/**
 * The Tag List as a list in the track table (issue #10028, rekordbox Tag List parity).
 *
 * The pane id `tag-list` addresses it, so the nav back-stack and the
 * `browser_select_playlist` performance command reach it like any other list.
 * It is not the MyTag navigation (`taglist:<name>` ids, kind `taglist`).
 * Pure: no Svelte, no DOM.
 */
import type { PlaylistNode } from '$lib/rb/library-types';

/** The one pane id of the Tag List. */
export const TAG_LIST_ID = 'tag-list';

/** True when a pane id addresses the Tag List. */
export function isTagListId(playlistId: string | null): boolean {
	return playlistId === TAG_LIST_ID;
}

/** The read-only list node that loads the Tag List into a pane. */
export function tagListNode(trackCount = 0): PlaylistNode {
	return {
		playlist_id: TAG_LIST_ID,
		name: 'Tag List',
		track_count: trackCount,
		broken_count: 0,
		kind: 'tag_list',
		children: []
	};
}

/** One table row per listed track still in the collection, in Tag List order. */
export function tagListRows<Item extends { stable_id: string }, Row>(
	listed: readonly { stable_id: string }[],
	items: readonly Item[],
	toRow: (item: Item, order: number) => Row
): Row[] {
	const byId = new Map(items.map((item) => [item.stable_id, item]));
	const rows: Row[] = [];
	for (const entry of listed) {
		const item = byId.get(entry.stable_id);
		if (item !== undefined) rows.push(toRow(item, rows.length + 1));
	}
	return rows;
}

/** The rail entry's tooltip, with the count when it is known. */
export function tagListRailTip(count: number | null, cap = 100): string {
	const held = count === null ? '' : ` (${count} of ${cap})`;
	return `Tag List${held} - a prep list: add tracks from the row menu, open it here, save it as a playlist or clear it`;
}

/** The rail badge text: the count, empty when the list is empty or unread. */
export function tagListBadge(count: number | null): string {
	return count === null || count === 0 ? '' : String(count);
}

/** The toast after an add: how many went on, how many were already listed. */
export function tagListAddedMessage(asked: number, added: number, count: number): string {
	const already = asked - added;
	const tail = already > 0 ? `, ${already} already listed` : '';
	return `Tag List: ${added} added${tail} (${count} listed)`;
}
