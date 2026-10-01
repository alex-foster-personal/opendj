const BROWSER_SORT_KEYS = [
	'order',
	'plays',
	'title',
	'artist',
	'key',
	'bpm',
	'rating',
	'comments',
	'time',
	'energy',
	'genre',
	'lyrics',
	'grid',
	'autoplay'
] as const;

export type SortKey = (typeof BROWSER_SORT_KEYS)[number];
export type SortDir = 1 | -1;

interface BrowserSortSnapshot {
	sort_key: SortKey | null;
	sort_dir: SortDir;
	visible_ids: readonly string[];
}

interface BrowserSortOwner {
	sort(key: SortKey): void;
	query(): BrowserSortSnapshot;
}

interface BrowserSortIpc {
	version: 1;
	sort(key: unknown): BrowserSortSnapshot;
	query(): BrowserSortSnapshot;
}

declare global {
	interface Window {
		musicDjToolsBrowserSort?: BrowserSortIpc;
	}
}

function _sortKey(value: unknown): SortKey {
	if (typeof value !== 'string' || !BROWSER_SORT_KEYS.includes(value as SortKey)) {
		throw new TypeError(`unknown browser sort key: ${String(value)}`);
	}
	return value as SortKey;
}

function _snapshot(owner: BrowserSortOwner): BrowserSortSnapshot {
	const value = owner.query();
	if (
		(value.sort_key !== null && !BROWSER_SORT_KEYS.includes(value.sort_key)) ||
		(value.sort_dir !== 1 && value.sort_dir !== -1) ||
		!Array.isArray(value.visible_ids) ||
		!value.visible_ids.every((id) => typeof id === 'string')
	) {
		throw new Error('browser sort owner returned an invalid snapshot');
	}
	return Object.freeze({ ...value, visible_ids: Object.freeze([...value.visible_ids]) });
}

/** Create the typed, lifecycle-bound browser sorting surface. */
function createBrowserSortIpc(owner: BrowserSortOwner): {
	ipc: BrowserSortIpc;
	dispose(): void;
} {
	let active = true;
	const assertActive = (): void => {
		if (!active) throw new Error('browser sort IPC session is unavailable');
	};
	const ipc: BrowserSortIpc = Object.freeze({
		version: 1 as const,
		sort: (key: unknown) => {
			assertActive();
			owner.sort(_sortKey(key));
			return _snapshot(owner);
		},
		query: () => {
			assertActive();
			return _snapshot(owner);
		}
	});
	return { ipc, dispose: () => { active = false; } };
}

/** Install exactly one active BrowserPanel sort owner on the browser window. */
export function installBrowserSortIpc(owner: BrowserSortOwner): () => void {
	if (typeof window === 'undefined') throw new Error('browser sort IPC requires a browser window');
	if (window.musicDjToolsBrowserSort !== undefined) throw new Error('browser sort IPC is already installed');
	const session = createBrowserSortIpc(owner);
	window.musicDjToolsBrowserSort = session.ipc;
	return () => {
		if (window.musicDjToolsBrowserSort !== session.ipc) {
			throw new Error('browser sort IPC ownership changed before cleanup');
		}
		session.dispose();
		delete window.musicDjToolsBrowserSort;
	};
}
