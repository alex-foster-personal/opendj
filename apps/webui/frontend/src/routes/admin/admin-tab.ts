export type AdminTab = 'kpi' | 'diagnostics';

export function adminTabFromUrl(url: URL): AdminTab {
	const tab = url.searchParams.get('tab');
	if (tab === 'diagnostics') return 'diagnostics';
	return 'kpi';
}
