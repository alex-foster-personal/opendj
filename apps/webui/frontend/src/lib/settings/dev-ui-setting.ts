import type { SettingDef } from './catalog-types';

/** V1 polish: one switch for developer-only pages and unbuilt settings. */
export const DEV_UI_SETTING: SettingDef = {
	id: 'show_dev_ui',
	label: 'Show developer pages',
	group: 'advanced',
	keywords: ['developer', 'dev', 'debug', 'admin', 'queues', 'progress', 'ledger', 'todo', 'unbuilt'],
	title: 'Show developer-only pages and unbuilt settings',
	detail:
		'When on, the sidebar shows the Admin, Progress ledger and Queues links, and the settings list shows unbuilt (todo) rows and the Rekordbox / djay Pro parity groups. Default off. Saved in this browser only.',
	implemented: true,
	control: { kind: 'boolean' }
};

/** The existing todo filter; only reachable while DEV_UI_SETTING is on. */
export const HIDE_TODO_SETTING: SettingDef = {
	id: 'hide_todo_settings',
	label: 'Hide todo / grayed settings',
	group: 'advanced',
	keywords: ['todo', 'gray', 'parity', 'hide', 'stub', 'placeholder'],
	title: 'Hide PARITY-TODO placeholder settings from the list',
	detail:
		'When on, only implemented settings appear in search results and category lists. Only shown while "Show developer pages" is on, since todo rows are hidden otherwise.',
	implemented: true,
	devOnly: true,
	control: { kind: 'boolean' }
};
