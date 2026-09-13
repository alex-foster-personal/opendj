export type AdminTab = 'kpi' | 'diagnostics' | 'playground';

export function adminTabFromUrl(url: URL): AdminTab {
	const tab = url.searchParams.get('tab');
	if (tab === 'diagnostics') return 'diagnostics';
	if (tab === 'playground') return 'playground';
	return 'kpi';
}
