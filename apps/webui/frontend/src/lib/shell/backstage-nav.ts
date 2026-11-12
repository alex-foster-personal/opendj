/**
 * Backstage sidebar (option A "Desk", the maintainer Tue 13 Oct 2026; ADR-NEW-backstage-shell).
 *
 * Behind the `shell.backstage` build flag (apps/feature_flags/store.py),
 * default OFF: with it off the shell renders SIDEBAR_NAV_LINKS
 * (sidebar-nav.ts) exactly as before. With it on the sidebar shows "Gig"
 * (today's /performance), a "Backstage" label with its groups, and, only
 * while "Show developer pages" (show_dev_ui) is on, a "Developer" divider
 * with Metrics / System / Tools.
 *
 * Pure (no prefs, flags or $page access) so the list, the inert rules and the
 * keyboard movement are unit testable (tests/unit/backstage-nav.test.mjs).
 */

import { isTextEntryTarget } from '$lib/keyboard/text-entry-target';

export const NOT_IMPLEMENTED_TITLE = 'not implemented - see PARITY-TODO';

export interface BackstageItem {
	id: string;
	label: string;
	/** Null when the item has no page yet: rendered inert. */
	href: string | null;
	title: string;
	/** 'unbuilt': no page exists yet. 'soon': planned, grayed out (AI group). */
	inert?: 'unbuilt' | 'soon';
}

export interface BackstageGroup {
	id: string;
	label: string;
	/** Developer-only: listed only while show_dev_ui is on. */
	dev?: boolean;
	items: BackstageItem[];
}

export const GIG_ITEM: BackstageItem = {
	id: 'gig',
	label: 'Gig',
	href: '/performance',
	title: 'The core Open DJ rig'
};

const admin = (p: string) => `/admin?p=${p}`;
const unbuilt = (id: string, label: string): BackstageItem => ({
	id,
	label,
	href: null,
	title: NOT_IMPLEMENTED_TITLE,
	inert: 'unbuilt'
});
const soon = (id: string, label: string): BackstageItem => ({
	id,
	label,
	href: null,
	title: `${label} - not built yet, see PARITY-TODO`,
	inert: 'soon'
});

export const BACKSTAGE_GROUPS: readonly BackstageGroup[] = [
	{
		id: 'overview',
		label: 'Overview',
		items: [
			{
				id: 'farm-jobs',
				label: 'Farm jobs',
				href: admin('farm-jobs'),
				title: 'Queued, running, and failed demucs, ANLZ cache, and cloud hydrate engine jobs'
			},
			{ id: 'status', label: 'Status', href: admin('status'), title: 'What needs a look across the app, from the live endpoints' }
		]
	},
	{
		id: 'library',
		label: 'Library',
		items: [
			{ id: 'library', label: 'Library', href: '/', title: 'Browse, search and edit the whole collection' },
			{ id: 'pairings', label: 'Pairings', href: '/pairings', title: 'Tracks marked as mixing well together' },
			{ id: 'smartlists', label: 'Smartlists', href: '/smartlists', title: 'Playlists built from rules' },
			{ id: 'wheel', label: 'Library wheel', href: '/library-wheel', title: 'The library laid out by key and tempo' },
			{ id: 'missing', label: 'Missing tracks', href: '/reconcile', title: 'Find and relink tracks whose audio file moved' },
			{ id: 'dedup', label: 'Dedup Review', href: '/dedup', title: 'Review copies of the same audio and pick a survivor' }
		]
	},
	{
		id: 'gen',
		label: 'Generate',
		items: [
			unbuilt('stems', 'Stems'),
			unbuilt('genres', 'Genres'),
			{ id: 'analysis', label: 'Analysis', href: '/analysis-backfill', title: 'BPM, key, beatgrid and waveform backfill' },
			unbuilt('vocals', 'Vocals')
		]
	},
	{
		id: 'sets',
		label: 'Sets',
		items: [
			{ id: 'sets', label: 'Sessions / REC', href: '/sets', title: 'Recorded sets and session history' },
			{ id: 'plays', label: 'Play analytics', href: '/play-analytics', title: 'What you played, when, and how often' }
		]
	},
	{
		id: 'lyrics',
		label: 'Lyrics',
		items: [
			{ id: 'sources', label: 'Sources', href: admin('sources'), title: 'The order lyric providers are tried in' },
			{ id: 'jobs', label: 'Jobs', href: admin('jobs'), title: 'Lyric generation jobs' },
			{ id: 'triage', label: 'Human review', href: admin('triage'), title: 'Label suspect lyrics, most suspect first' },
			{ id: 'coverage', label: 'Coverage', href: admin('coverage'), title: 'How much of the library has synced lyrics' }
		]
	},
	{
		id: 'ai',
		label: 'AI',
		items: [soon('aistudio', 'AI Studio'), soon('aiwb', 'AI Workbench'), soon('bench', 'Benchmarking')]
	},
	{
		id: 'sync',
		label: 'Sync',
		items: [{ id: 'cloudsync', label: 'CloudSync', href: '/cloudsync', title: 'Sync your library between machines' }]
	},
	{
		id: 'prefs',
		label: 'Settings',
		items: [
			{
				id: 'settings',
				label: 'Settings',
				href: '/settings',
				title: 'Settings - Cmd+, (Ctrl+, on Windows/Linux) opens the quick settings overlay from anywhere'
			},
			{ id: 'setup', label: 'Setup', href: '/setup', title: 'First-run wizard: library folders, audio devices, analysis' },
			{ id: 'plugins', label: 'Plugins', href: '/plugins', title: 'Install, enable and disable plugins' }
		]
	},
	{
		id: 'metrics',
		label: 'Metrics',
		dev: true,
		items: [
			{ id: 'perf', label: 'Performance KPIs', href: admin('perf'), title: 'Latency and RAM program KPIs' },
			{ id: 'farm', label: 'Stem farm', href: admin('farm'), title: 'Demucs farm KPI ledger' },
			{ id: 'quality', label: 'Code quality', href: admin('quality'), title: 'The quality ratchet the merge gate enforces' }
		]
	},
	{
		id: 'system',
		label: 'System',
		dev: true,
		items: [
			{ id: 'health', label: 'Health', href: admin('health'), title: 'Daemon health and capability probe' },
			{ id: 'bus', label: 'Event bus', href: admin('bus'), title: 'The live event stream from the engine' },
			{ id: 'flags', label: 'Flags', href: admin('flags'), title: 'Entitlements and build flags for this session' },
			{ id: 'preflight', label: 'Preflight', href: admin('preflight'), title: 'The boot gate checks, polled live' },
			{ id: 'ports', label: 'Worktree ports', href: admin('ports'), title: 'Which worktree owns which port' }
		]
	},
	{
		id: 'tools',
		label: 'Tools',
		dev: true,
		items: [
			{ id: 'api', label: 'API', href: admin('api'), title: 'Call /api/v1/* as the app does' },
			{ id: 'sql', label: 'SQL', href: admin('sql'), title: 'Read-only SQL against state.db' },
			{ id: 'queues', label: 'Queues', href: '/queues', title: 'Background work queues' },
			{ id: 'progress', label: 'Progress tree', href: '/progress-tree', title: 'The feature ledger and its dependency graph' }
		]
	}
];

export interface BackstageNav {
	gig: BackstageItem;
	backstage: BackstageGroup[];
	/** Empty unless showDev. */
	developer: BackstageGroup[];
}

/** The sections to render: developer groups only while showDev is on. */
export function backstageNav(showDev: boolean): BackstageNav {
	return {
		gig: GIG_ITEM,
		backstage: BACKSTAGE_GROUPS.filter((g) => g.dev !== true),
		developer: showDev ? BACKSTAGE_GROUPS.filter((g) => g.dev === true) : []
	};
}

/** Whether `item` is the page on screen. /admin?p=x items match by panel id. */
export function isCurrentBackstageItem(item: BackstageItem, url: URL): boolean {
	if (item.href === null) return false;
	const target = new URL(item.href, url.origin);
	if (target.searchParams.has('p')) {
		return url.pathname === target.pathname && url.searchParams.get('p') === target.searchParams.get('p');
	}
	if (target.pathname === '/') return url.pathname === '/';
	return url.pathname === target.pathname || url.pathname.startsWith(`${target.pathname}/`);
}

/**
 * W/A/S/D are the arrow keys (Up/Left/Down/Right) in the nav, with modifiers
 * preserved. Returns the arrow key name, or null for any other key.
 */
export function navArrowKey(key: string): 'ArrowUp' | 'ArrowDown' | 'ArrowLeft' | 'ArrowRight' | null {
	switch (key) {
		case 'ArrowUp':
		case 'w':
		case 'W':
			return 'ArrowUp';
		case 'ArrowDown':
		case 's':
		case 'S':
			return 'ArrowDown';
		case 'ArrowLeft':
		case 'a':
		case 'A':
			return 'ArrowLeft';
		case 'ArrowRight':
		case 'd':
		case 'D':
			return 'ArrowRight';
		default:
			return null;
	}
}

/**
 * Next focus index among `count` focusable items. Up/Left step back one,
 * Down/Right step forward one; with a modifier (Shift, Ctrl, Alt, Meta) the
 * step jumps to the first/last item. Clamped, never wraps.
 */
export function nextNavIndex(
	current: number,
	count: number,
	arrow: 'ArrowUp' | 'ArrowDown' | 'ArrowLeft' | 'ArrowRight',
	modified: boolean
): number {
	if (count <= 0) return -1;
	const back = arrow === 'ArrowUp' || arrow === 'ArrowLeft';
	if (modified) return back ? 0 : count - 1;
	const from = current < 0 ? (back ? count : -1) : current;
	return Math.min(count - 1, Math.max(0, from + (back ? -1 : 1)));
}

/**
 * Shared row keyboard handler for the Backstage tables: W/A/S/D or the arrows
 * move focus among the `rowSelector` matches inside `container`. Typing in a
 * text field wins. Returns true when focus moved (the event is consumed).
 */
export function moveRowFocus(event: KeyboardEvent, container: HTMLElement, rowSelector: string): boolean {
	if (isTextEntryTarget(event.target)) return false;
	const arrow = navArrowKey(event.key);
	if (arrow === null) return false;
	const rows = Array.from(container.querySelectorAll<HTMLElement>(rowSelector));
	const current = rows.indexOf(document.activeElement as HTMLElement);
	const modified = event.shiftKey || event.ctrlKey || event.altKey || event.metaKey;
	const next = nextNavIndex(current, rows.length, arrow, modified);
	if (next < 0) return false;
	event.preventDefault();
	event.stopPropagation();
	rows[next].focus();
	return true;
}
