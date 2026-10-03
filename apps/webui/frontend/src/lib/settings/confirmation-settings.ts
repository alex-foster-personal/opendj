/**
 * The Confirmations settings group: every remembered destructive/move prompt
 * choice, in catalog order. Spread into SETTINGS_CATALOG by catalog.ts.
 */

import type { SettingDef } from './catalog-types';

export const CONFIRMATION_SETTINGS: readonly SettingDef[] = [
	{
		id: 'confirm.dblclick_load_play',
		label: 'Confirm double-click Load+play',
		group: 'confirmations',
		keywords: ['confirm', 'double', 'click', 'load', 'play', 'prompt'],
		title: 'Ask before Load+play on double-click',
		detail: 'Off skips the prompt (do this every time). Missing/default means ask.',
		implemented: true,
		control: { kind: 'boolean' }
	},
	{
		id: 'confirm.delete_playlist',
		label: 'Confirm playlist delete',
		group: 'confirmations',
		keywords: ['confirm', 'delete', 'playlist', 'remove', 'prompt'],
		title: 'Ask before deleting a playlist',
		detail: 'Off skips the destructive confirm forever. Missing/default means ask.',
		implemented: true,
		control: { kind: 'boolean' }
	},
	{
		id: 'confirm.playlist_drop_mode',
		label: 'Playlist drop default',
		group: 'confirmations',
		keywords: ['confirm', 'drop', 'playlist', 'add', 'move', 'drag'],
		title: 'Remembered add vs move for playlist drops',
		detail: 'Choose Ask to clear the remembered choice and re-prompt on the next drop.',
		implemented: true,
		control: {
			kind: 'enum',
			options: [
				{ value: 'ask', label: 'Ask each time' },
				{ value: 'add', label: 'Always add' },
				{ value: 'move', label: 'Always move' }
			]
		}
	}
];
