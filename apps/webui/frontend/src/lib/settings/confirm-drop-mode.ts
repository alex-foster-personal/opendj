/**
 * Settings row for the remembered playlist-drop choice (LIBUX-32, pin
 * 36e2e2a7ccff). The pref stores only a remembered choice ('add' | 'move');
 * no stored value means "ask every time", which the row spells `ask`.
 */
import type { SettingDef } from './catalog-types';

export type DropModeSetting = 'ask' | 'add' | 'move';

export const DROP_MODE_SETTING_OPTIONS: { value: DropModeSetting; label: string }[] = [
	{ value: 'ask', label: 'Ask every time' },
	{ value: 'add', label: 'Always add' },
	{ value: 'move', label: 'Always move' }
];

export function dropModeSettingValue(remembered: 'add' | 'move' | undefined): DropModeSetting {
	return remembered ?? 'ask';
}

/** undefined clears the remembered choice, so the prompt returns. */
export function dropModePrefFromSetting(value: unknown): 'add' | 'move' | undefined {
	if (value === 'ask') return undefined;
	else if (value === 'add' || value === 'move') return value;
	throw new Error(`confirm.playlist_drop_mode must be ask|add|move, got ${String(value)}`);
}

/** The Settings > Confirmations rows, in display order. */
export const CONFIRM_SETTINGS: SettingDef[] = [
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
		label: 'Dropping tracks on a playlist',
		group: 'confirmations',
		keywords: ['confirm', 'drop', 'drag', 'playlist', 'add', 'move', 'remember', 'prompt', 'reset'],
		title: 'What dropping tracks on a playlist does: ask, always add, or always move',
		detail:
			'The drop prompt can remember your answer. This row shows what it remembered and lets you change it; Ask every time clears the remembered answer so the prompt comes back.',
		implemented: true,
		control: { kind: 'enum', options: DROP_MODE_SETTING_OPTIONS }
	}
];
