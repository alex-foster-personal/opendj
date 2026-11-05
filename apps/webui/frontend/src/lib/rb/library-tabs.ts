/**
 * Library browser tabs (LIBUX tabs): the pure tab model behind PaneTabs.
 *
 * The open tabs persist as the `library_tabs` ui-pref (GET/PUT
 * /api/v1/ui-prefs); agents drive them with POST
 * /api/v1/ui-prefs/library-tabs (list/open/switch/close). Mirrors
 * apps/webui/server/library_tabs_prefs.py. Runeless so node:test loads it.
 */

export type LibraryTabShortcut = 'new' | 'close' | 'next' | 'prev';

export interface LibraryTabPref {
	playlist_id: string | null;
	name: string;
	search: string;
	sort_key: string | null;
	sort_dir: 1 | -1;
}

export interface LibraryTabsPref {
	tabs: LibraryTabPref[];
	active: number;
}

/** The pane fields a tab persists (a PaneStore satisfies this). */
export interface LibraryTabSource {
	playlist_id: string | null;
	title: string;
	search: string;
	sort_key: string | null;
	sort_dir: 1 | -1;
}

/** Active index after closing `index`, or null: the last tab never closes. */
export function activeAfterClose(count: number, active: number, index: number): number | null {
	if (count <= 1) return null;
	if (index < active || active === count - 1) return Math.max(active - 1, 0);
	return active;
}

/** Next/previous tab with wrap-around. */
export function cycleTab(count: number, active: number, step: 1 | -1): number {
	return (active + step + count) % count;
}

/** Cmd/Ctrl+T new, +W close, +Shift+] next, +Shift+[ prev (rekordbox/Serato/browser idiom). */
export function libraryTabShortcut(event: {
	key: string;
	metaKey: boolean;
	ctrlKey: boolean;
	shiftKey: boolean;
	altKey: boolean;
}): LibraryTabShortcut | null {
	if (!(event.metaKey || event.ctrlKey) || event.altKey) return null;
	const key = event.key.toLowerCase();
	if (event.shiftKey) {
		if (key === ']' || key === '}') return 'next';
		else if (key === '[' || key === '{') return 'prev';
		return null;
	}
	if (key === 't') return 'new';
	else if (key === 'w') return 'close';
	return null;
}

export function serializeLibraryTabs(panes: readonly LibraryTabSource[], active: number): LibraryTabsPref {
	return {
		tabs: panes.map((p) => ({
			playlist_id: p.playlist_id,
			name: p.title,
			search: p.search,
			sort_key: p.sort_key,
			sort_dir: p.sort_dir
		})),
		active
	};
}

export function sameLibraryTabs(a: LibraryTabsPref, b: LibraryTabsPref): boolean {
	return JSON.stringify(a) === JSON.stringify(b);
}

/** The saved tabs still worth restoring (unknown playlists dropped), or null for none. */
export function restorableLibraryTabs(
	pref: LibraryTabsPref,
	isKnownPlaylist: (playlistId: string) => boolean
): LibraryTabsPref | null {
	const activeId = pref.tabs[pref.active]?.playlist_id;
	const tabs = pref.tabs.filter((t) => t.playlist_id === null || isKnownPlaylist(t.playlist_id));
	if (!tabs.some((t) => t.playlist_id !== null)) return null;
	const kept = tabs.findIndex((t) => t.playlist_id === activeId);
	return { tabs, active: kept >= 0 ? kept : Math.min(pref.active, tabs.length - 1) };
}
