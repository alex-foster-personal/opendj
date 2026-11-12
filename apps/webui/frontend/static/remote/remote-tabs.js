import { renderRequestsTab } from './requests-tab-render.js';

export const REMOTE_TABS = [{ id: 'requests', label: 'Requests', render: renderRequestsTab }];

export function renderTabBar(selectedId = 'requests', badges = {}) {
	const buttons = REMOTE_TABS.map((tab) => {
		const current = tab.id === selectedId ? ' aria-current="page"' : '';
		const count = badges[tab.id];
		const badge = count > 0 ? `<span class="tab-badge">${count}</span>` : '';
		return `<button type="button" data-tab="${tab.id}"${current}>${tab.label}${badge}</button>`;
	}).join('');
	return `<nav class="remote-tabs" aria-label="Phone remote">${buttons}</nav>`;
}
