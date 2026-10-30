export type AdminTab = 'kpi' | 'diagnostics' | 'playground' | 'title-fix';

export function adminTabFromUrl(url: URL): AdminTab {
	const tab = url.searchParams.get('tab');
	if (tab === 'diagnostics') return 'diagnostics';
	if (tab === 'playground') return 'playground';
	if (tab === 'title-fix') return 'title-fix';
	return 'kpi';
}
