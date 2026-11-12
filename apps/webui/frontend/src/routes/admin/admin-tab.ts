export type AdminTab =
	| 'kpi'
	| 'lyrics'
	| 'quality'
	| 'diagnostics'
	| 'playground'
	| 'spotify'
	| 'title-fix';

export function adminTabFromUrl(url: URL): AdminTab {
	const tab = url.searchParams.get('tab');
	if (tab === 'lyrics') return 'lyrics';
	if (tab === 'quality') return 'quality';
	if (tab === 'diagnostics') return 'diagnostics';
	if (tab === 'playground') return 'playground';
	if (tab === 'spotify') return 'spotify';
	if (tab === 'title-fix') return 'title-fix';
	return 'kpi';
}

/**
 * Backstage (shell.backstage): one /admin panel at a time, addressed by
 * `?p=<id>`. Ids match the Backstage sidebar items (lib/shell/backstage-nav.ts).
 * Several ids share one existing component (health/bus/ports are the
 * Diagnostics panel, api/sql the Playground) because the panels are reused
 * unchanged. An unknown id is refused, never silently mapped.
 */
export const ADMIN_PANEL_IDS = [
	'status',
	'farm-jobs',
	'sources',
	'jobs',
	'triage',
	'coverage',
	'perf',
	'farm',
	'quality',
	'health',
	'bus',
	'flags',
	'preflight',
	'ports',
	'api',
	'sql'
] as const;
export type AdminPanel = (typeof ADMIN_PANEL_IDS)[number];

export type AdminPanelPick = { kind: 'none' } | { kind: 'panel'; id: AdminPanel } | { kind: 'unknown'; id: string };

export function adminPanelFromUrl(url: URL): AdminPanelPick {
	const p = url.searchParams.get('p');
	if (p === null || p === '') return { kind: 'none' };
	return (ADMIN_PANEL_IDS as readonly string[]).includes(p)
		? { kind: 'panel', id: p as AdminPanel }
		: { kind: 'unknown', id: p };
}
