/**
 * "Save this order" (LIBUX-8678): the order a playlist is shown in, after any
 * column sort and with its sections arranged, becomes its stored numbering.
 * One POST to items:save-order, one undo step; the route, `opendj playlist
 * save-order` and the `playlist_save_order` MCP tool are the same write.
 */
import { api } from '$lib/api/client';

type HasItemId = { item_id: string | null };

/** Every live item_id in display order: what the table shows first, then any row
 * a filter or a collapsed section hid, in its stored place. Throws on a row with
 * no item_id, since the server could not place it. */
export function saveOrderItemIds(
	displayed: readonly HasItemId[],
	all: readonly HasItemId[]
): string[] {
	const idOf = (row: HasItemId): string => {
		if (row.item_id === null) throw new Error('save this order: a row has no item_id');
		return row.item_id;
	};
	const shown = displayed.map(idOf);
	const seen = new Set(shown);
	return [...shown, ...all.map(idOf).filter((id) => !seen.has(id))];
}

/** True when the shown order differs from the stored one, so the button is worth showing. */
export function orderDiffers(displayed: readonly HasItemId[], all: readonly HasItemId[]): boolean {
	return displayed.some((row, i) => row.item_id !== all[i]?.item_id);
}

/** POST items:save-order under the playlist's current etag. */
export async function savePlaylistOrder(
	playlistId: string,
	etag: string,
	itemIds: string[]
): Promise<void> {
	await api.POST('/api/v1/playlists/{playlist_id}/items:save-order', {
		params: { path: { playlist_id: playlistId }, header: { 'If-Match': etag } },
		body: { item_ids: itemIds, descending: false }
	});
}
