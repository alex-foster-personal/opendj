/**
 * Library tabs controller (LIBUX tabs): close / new / cycle / shortcuts on the
 * browser's pane tabs, plus persistence through the `library_tabs` ui-pref and
 * replay of an agent's POST /api/v1/ui-prefs/library-tabs. BrowserPanel owns
 * the panes and injects how to load one; the pure rules live in
 * $lib/rb/library-tabs.
 */
import { api, unwrap } from '$lib/api/rb-http';
import { isPageGoingAway } from '$lib/api/lifecycle-abort';
import type { PlaylistNode } from '$lib/rb/library-types';
import {
	activeAfterClose,
	cycleTab,
	libraryTabShortcut,
	restorableLibraryTabs,
	sameLibraryTabs,
	serializeLibraryTabs,
	type LibraryTabsPref
} from '$lib/rb/library-tabs';
import { BROWSER_SORT_KEYS, type SortKey } from './browser-sort-ipc';
import { canAddPaneSlot } from './pane-tabs';
import type { PaneStore } from './pane-contract.svelte';

const PERSIST_DEBOUNCE_MS = 400;

export interface LibraryTabsDeps {
	/** The browser's $state pane array: push/splice re-render the strip. */
	panes: PaneStore[];
	activePane: () => number;
	setActivePane: (index: number) => void;
	newPane: () => PaneStore;
	isKnownPlaylist: (playlistId: string) => boolean;
	nodeFor: (playlistId: string, name: string) => PlaylistNode | null;
	loadPane: (pane: PaneStore, node: PlaylistNode) => Promise<void>;
	report: (message: string) => void;
	/** A notice that is not a failure (a saved tab's list was deleted since). */
	warn: (message: string) => void;
}

async function _readLibraryTabs(): Promise<LibraryTabsPref> {
	const body = (await unwrap(api.GET('/api/v1/ui-prefs'))) as { library_tabs?: LibraryTabsPref };
	if (body.library_tabs === undefined) {
		throw new Error('GET /api/v1/ui-prefs: library_tabs is missing from the response');
	}
	return body.library_tabs;
}

function _sortKeyOf(value: string | null): SortKey | null {
	return value !== null && (BROWSER_SORT_KEYS as readonly string[]).includes(value) ? (value as SortKey) : null;
}

export function createLibraryTabsController(deps: LibraryTabsDeps) {
	const { panes } = deps;
	let restored = false;
	let lastSynced: LibraryTabsPref | null = null;
	let cleanupDue = false;
	let persistTimer: ReturnType<typeof setTimeout> | null = null;

	function close(index: number): void {
		const next = activeAfterClose(panes.length, deps.activePane(), index);
		if (next === null) return; // the last tab never closes
		panes.splice(index, 1);
		deps.setActivePane(next);
	}

	/** Cmd+T: a new tab on the current list (rekordbox/Serato duplicate-tab idiom). */
	function openNew(): void {
		if (!canAddPaneSlot(panes.length)) {
			deps.report(`all ${panes.length} library tabs are open - close one first`);
			return;
		}
		const current = panes[deps.activePane()];
		const pane = deps.newPane();
		panes.push(pane);
		deps.setActivePane(panes.length - 1);
		const node = current.playlist_id === null ? null : deps.nodeFor(current.playlist_id, current.title);
		if (node !== null) void deps.loadPane(pane, node);
	}

	function onKeydown(event: KeyboardEvent): void {
		const action = libraryTabShortcut(event);
		if (action === null) return;
		event.preventDefault();
		if (action === 'new') openNew();
		else if (action === 'close') close(deps.activePane());
		else if (action === 'next') deps.setActivePane(cycleTab(panes.length, deps.activePane(), 1));
		else if (action === 'prev') deps.setActivePane(cycleTab(panes.length, deps.activePane(), -1));
	}

	async function _apply(pref: LibraryTabsPref): Promise<boolean> {
		const plan = restorableLibraryTabs(pref, deps.isKnownPlaylist);
		const dropped = pref.tabs.length - (plan?.tabs.length ?? 0);
		// LIBUX-8677b: a list deleted since the tabs were saved is news, not a failure.
		if (dropped > 0) deps.warn(`${dropped} saved library tab(s) name a list that no longer exists`);
		cleanupDue = dropped > 0;
		if (plan === null) return false;
		// LIBUX-8677b: a dropped tab is saved away, so the warning shows once. Leaving
		// lastSynced on the saved pref (not the cleaned plan) makes persistSoon write
		// the cleaned tabs; marking the plan as synced kept the deleted list saved and
		// warned on every boot.
		lastSynced = dropped > 0 ? pref : plan;
		panes.splice(plan.tabs.length);
		while (panes.length < plan.tabs.length) panes.push(deps.newPane());
		plan.tabs.forEach((t, i) => {
			if (t.playlist_id === null && panes[i].playlist_id !== null) panes[i] = deps.newPane();
		});
		deps.setActivePane(plan.active);
		const order = [plan.active, ...plan.tabs.keys()].filter((i, at, all) => all.indexOf(i) === at);
		for (const i of order) {
			const tab = plan.tabs[i];
			const pane = panes[i];
			if (tab.playlist_id !== null && pane.playlist_id !== tab.playlist_id) {
				const node = deps.nodeFor(tab.playlist_id, tab.name);
				if (node !== null) await deps.loadPane(pane, node);
			}
			pane.setSearch(tab.search);
			pane.sort_key = _sortKeyOf(tab.sort_key);
			pane.sort_dir = tab.sort_dir;
		}
		return true;
	}

	/** Boot: reopen the saved tabs. False = nothing restored; the caller boots one pane.
	 * `apply` false (a deep link owns the first pane) only arms persistence. */
	async function restore(apply: boolean): Promise<boolean> {
		let reopened = false;
		try {
			reopened = apply && (await _apply(await _readLibraryTabs()));
		} catch (error) {
			if (!isPageGoingAway()) deps.report(`restoring library tabs failed: ${String(error)}`);
		} finally {
			restored = true;
		}
		// A restore that dropped a tab may leave the panes exactly as they were, so
		// no tab-state change follows to save the cleaned set: save it here.
		if (cleanupDue) {
			cleanupDue = false;
			persistSoon(serializeLibraryTabs(panes, deps.activePane()));
		}
		return reopened;
	}

	/** A ui_prefs change event: replay a tab change made elsewhere (an agent). */
	async function onRemoteChange(): Promise<void> {
		if (!restored) return;
		const pref = await _readLibraryTabs();
		if (lastSynced !== null && sameLibraryTabs(pref, lastSynced)) return;
		if (sameLibraryTabs(pref, serializeLibraryTabs(panes, deps.activePane()))) return;
		await _apply(pref);
	}

	/** Called on every tab-state change; writes once things settle. */
	function persistSoon(pref: LibraryTabsPref): void {
		if (!restored || (lastSynced !== null && sameLibraryTabs(pref, lastSynced))) return;
		if (persistTimer !== null) clearTimeout(persistTimer);
		persistTimer = setTimeout(() => {
			persistTimer = null;
			lastSynced = pref;
			api.PUT('/api/v1/ui-prefs', { body: { library_tabs: pref } }).then(
				(r) => {
					if (r.error !== undefined && !isPageGoingAway()) {
						deps.report(`saving library tabs failed: ${JSON.stringify(r.error)}`);
					}
				},
				(error: unknown) => {
					if (!isPageGoingAway()) deps.report(`saving library tabs failed: ${String(error)}`);
				}
			);
		}, PERSIST_DEBOUNCE_MS);
	}

	function dispose(): void {
		if (persistTimer !== null) clearTimeout(persistTimer);
	}

	return { close, openNew, onKeydown, restore, onRemoteChange, persistSoon, dispose };
}
