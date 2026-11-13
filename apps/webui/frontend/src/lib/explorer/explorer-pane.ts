/**
 * The Explorer source in the browser (issue #9747, rekordbox Explorer parity, mimicked).
 *
 * The tree slot shows the collection as a folder tree of its stored file paths;
 * opening a folder loads its tracks into the active pane as a read-only list. A
 * folder is addressed as the pane id `explorer:<path>` (its own tracks) or
 * `explorer-all:<path>` (every track below it), so the nav back-stack and the
 * `browser_select_playlist` performance command reach it like any other list.
 * Pure: no Svelte, no DOM.
 */
import type { PlaylistNode } from '$lib/rb/library-types';
import type { FolderNode } from './explorer-api';

const DIRECT_PREFIX = 'explorer:';
const RECURSIVE_PREFIX = 'explorer-all:';

/** The rail entry's tooltip. */
export const EXPLORER_TIP =
	'Explorer - browse the collection as folders on disk; open a folder to list its tracks';

export interface ExplorerFolder {
	path: string;
	recursive: boolean;
}

/** One visible row of the folder tree, in display order. */
export interface ExplorerRow {
	node: FolderNode;
	depth: number;
	expandable: boolean;
	expanded: boolean;
}

/** The pane id that opens one folder: `explorer:/Music/House`, or `explorer-all:` with every folder below. */
export function explorerFolderId(path: string, recursive = false): string {
	if (path === '') throw new Error('explorer: a folder path is required');
	return `${recursive ? RECURSIVE_PREFIX : DIRECT_PREFIX}${path}`;
}

/** The folder a pane id addresses, or null when the id is not an Explorer folder. */
export function explorerFolder(playlistId: string | null): ExplorerFolder | null {
	const id = playlistId ?? '';
	for (const [prefix, recursive] of [
		[RECURSIVE_PREFIX, true],
		[DIRECT_PREFIX, false]
	] as const) {
		if (id.startsWith(prefix) && id.length > prefix.length)
			return { path: id.slice(prefix.length), recursive };
	}
	return null;
}

/** The last segment of a folder path (`House` for `/Music/House`, the root itself for `/` or `C:\`). */
export function folderLabel(path: string): string {
	const segments = path.split(/[\\/]/).filter((segment) => segment !== '');
	return segments.length === 0 || /^[A-Za-z]:$/.test(path.replace(/[\\/]+$/, ''))
		? path
		: segments[segments.length - 1];
}

/** The read-only list node that loads one folder into a pane. */
export function explorerFolderNode(folder: ExplorerFolder, trackCount = 0): PlaylistNode {
	return {
		playlist_id: explorerFolderId(folder.path, folder.recursive),
		name: `${folderLabel(folder.path)}${folder.recursive ? ' (all)' : ''}`,
		track_count: trackCount,
		broken_count: 0,
		kind: 'explorer',
		children: []
	};
}

/** One table row per listed track still in the collection, in the route's file-name order. */
export function explorerFolderRows<Item extends { stable_id: string }, Row>(
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

/** The tree flattened to the rows on screen: each root, and the children of every expanded folder. */
export function visibleFolderRows(
	roots: readonly FolderNode[],
	expanded: ReadonlySet<string>
): ExplorerRow[] {
	const rows: ExplorerRow[] = [];
	const walk = (node: FolderNode, depth: number): void => {
		const open = expanded.has(node.path);
		rows.push({ node, depth, expandable: node.children.length > 0, expanded: open });
		if (open) for (const child of node.children) walk(child, depth + 1);
	};
	for (const root of roots) walk(root, 0);
	return rows;
}

/** The folders to expand so `path` is on screen: every ancestor in the tree, root first. */
export function ancestorPaths(roots: readonly FolderNode[], path: string): string[] {
	const trail: string[] = [];
	const find = (node: FolderNode): boolean => {
		if (node.path === path) return true;
		trail.push(node.path);
		if (node.children.some(find)) return true;
		trail.pop();
		return false;
	};
	return roots.some(find) ? trail : [];
}

/** A folder row's count: tracks directly inside, and the total below when that differs. */
export function folderCountLabel(node: FolderNode): string {
	return node.direct_track_count === node.track_count
		? String(node.track_count)
		: `${node.direct_track_count} / ${node.track_count}`;
}
