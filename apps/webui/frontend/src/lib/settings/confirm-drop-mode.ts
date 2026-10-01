/**
 * Settings row for the remembered playlist-drop choice (LIBUX-31, pin
 * 36e2e2a7ccff). The pref stores only a remembered choice ('add' | 'move');
 * no stored value means "ask every time", which the row spells `ask`.
 */
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
