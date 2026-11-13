/**
 * Tag List actions shared by the browser and the performance bus (issue #10028).
 *
 * One count value backs the rail badge, so the `tag_list_add` performance command
 * and the browser's Remove / Save as playlist / Clear buttons always agree. The
 * HTTP routes (#14510) are the source of truth; this module only re-reads them.
 */
import { pushToast } from '$lib/stores.svelte';
import {
	addToTagList,
	clearTagList,
	fetchTagList,
	removeFromTagList,
	saveTagListAsPlaylist
} from './tag-list-api';
import { tagListAddedMessage } from './tag-list-pane';

export { fetchTagList, clearTagList, saveTagListAsPlaylist };
export {
	isTagListId,
	tagListBadge,
	tagListNode,
	tagListRailTip,
	tagListRows,
	TAG_LIST_ID
} from './tag-list-pane';

export const tagListCount = $state<{ value: number | null }>({ value: null });

/** Re-read the count from GET /taglist; a failure leaves the badge unknown and is logged. */
export async function refreshTagListCount(): Promise<void> {
	try {
		tagListCount.value = (await fetchTagList()).count;
	} catch (error) {
		tagListCount.value = null;
		console.error('[tag-list] count read failed:', error);
	}
}

/** The `tag_list_add` command: append the rows, update the badge, say what happened. */
export async function runTagListAdd(stableIds: readonly string[]): Promise<void> {
	const result = await addToTagList(stableIds);
	tagListCount.value = result.count;
	pushToast(
		tagListAddedMessage(new Set(stableIds).size, result.added.length, result.count),
		'info'
	);
}

/** Take each selected track off the Tag List, one DELETE per id, then update the badge. */
export async function removeTagListTracks(stableIds: readonly string[]): Promise<void> {
	for (const stableId of new Set(stableIds)) {
		tagListCount.value = (await removeFromTagList(stableId)).count;
	}
}
