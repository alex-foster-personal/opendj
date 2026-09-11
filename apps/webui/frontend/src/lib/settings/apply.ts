/**
 * Allowlisted setting mutators. Used by the overlay controls and AI apply.
 * Unknown keys throw (fail-loud).
 */
import {
	DECK_LAYOUT_DURATIONS_MS,
	setAutoPlayEnabled,
	setAutoPlayEnforceOrder,
	setAutoPlayMaximizeReach,
	setAutoSyncDestination,
	setBeatSyncMax,
	setConfirmPref,
	setDeckLayoutAnimate,
	setDeckLayoutDurationMs,
	setDeckLayoutMode,
	setHideBrokenLinks,
	setHideTodoSettings,
	setJogRadialWaveform,
	setLibraryDensity,
	setNextOnlyFilter,
	setTechnicallyWorkingAnimate,
	setTheme,
	uiPrefs,
	type AutoSyncDestination,
	type DeckLayoutDurationMs,
	type DeckLayoutMode,
	type LibraryDensity,
	type UiTheme
} from '$lib/rb/prefs.svelte';
import {
	setWheelSensitivity,
	wheelSensitivity,
	type WheelInputKind
} from '$lib/rb/wheel-adjust';

export const ALLOWED_SETTING_KEYS = [
	'theme',
	'hide_broken_links',
	'library_density',
	'beat_sync_max',
	'auto_play_enabled',
	'auto_play_enforce_order',
	'auto_play_maximize_reach',
	'next_only_filter',
	'hide_todo_settings',
	'technically_working_animate',
	'jog_radial_waveform',
	'deck_layout',
	'deck_layout_animate',
	'deck_layout_duration_ms',
	'auto_sync.rekordbox',
	'auto_sync.djay',
	'auto_sync.open_dj',
	'confirm.delete_playlist',
	'confirm.dblclick_load_play',
	'wheel_sensitivity.mouse',
	'wheel_sensitivity.trackpad'
] as const;

export type AllowedSettingKey = (typeof ALLOWED_SETTING_KEYS)[number];

export function isAllowedSettingKey(key: string): key is AllowedSettingKey {
	return (ALLOWED_SETTING_KEYS as readonly string[]).includes(key);
}

export type SettingValue = boolean | string;

export function readSettingValue(key: AllowedSettingKey): SettingValue {
	switch (key) {
		case 'theme':
			return uiPrefs.theme;
		case 'hide_broken_links':
			return uiPrefs.hide_broken_links;
		case 'library_density':
			return uiPrefs.library_density;
		case 'beat_sync_max':
			return uiPrefs.beat_sync_max;
		case 'auto_play_enabled':
			return uiPrefs.auto_play_enabled;
		case 'auto_play_enforce_order':
			return uiPrefs.auto_play_enforce_order;
		case 'auto_play_maximize_reach':
			return uiPrefs.auto_play_maximize_reach;
		case 'next_only_filter':
			return uiPrefs.next_only_filter;
		case 'hide_todo_settings':
			return uiPrefs.hide_todo_settings;
		case 'technically_working_animate':
			return uiPrefs.technically_working_animate;
		case 'jog_radial_waveform':
			return uiPrefs.jog_radial_waveform;
		case 'deck_layout':
			return uiPrefs.deck_layout;
		case 'deck_layout_animate':
			return uiPrefs.deck_layout_animate;
		case 'deck_layout_duration_ms':
			return String(uiPrefs.deck_layout_duration_ms);
		case 'auto_sync.rekordbox':
			return uiPrefs.auto_sync.rekordbox;
		case 'auto_sync.djay':
			return uiPrefs.auto_sync.djay;
		case 'auto_sync.open_dj':
			return uiPrefs.auto_sync.open_dj;
		case 'confirm.delete_playlist':
			return uiPrefs.confirm.delete_playlist !== false;
		case 'confirm.dblclick_load_play':
			return uiPrefs.confirm.dblclick_load_play !== false;
		case 'wheel_sensitivity.mouse':
			return String(wheelSensitivity().mouse);
		case 'wheel_sensitivity.trackpad':
			return String(wheelSensitivity().trackpad);
	}
}

export function applySettingChange(key: string, value: SettingValue): void {
	if (!isAllowedSettingKey(key)) {
		throw new Error(`unknown or disallowed setting key: ${key}`);
	}
	switch (key) {
		case 'theme': {
			if (value !== 'dark' && value !== 'light') {
				throw new Error(`theme must be dark|light, got ${String(value)}`);
			}
			setTheme(value as UiTheme);
			return;
		}
		case 'hide_broken_links':
			setHideBrokenLinks(_asBool(value, key));
			return;
		case 'library_density': {
			if (value !== 'compact' && value !== 'cosy') {
				throw new Error(`library_density must be compact|cosy, got ${String(value)}`);
			}
			setLibraryDensity(value as LibraryDensity);
			return;
		}
		case 'beat_sync_max':
			setBeatSyncMax(_asBool(value, key));
			return;
		case 'auto_play_enabled':
			setAutoPlayEnabled(_asBool(value, key));
			return;
		case 'auto_play_enforce_order':
			setAutoPlayEnforceOrder(_asBool(value, key));
			return;
		case 'auto_play_maximize_reach':
			setAutoPlayMaximizeReach(_asBool(value, key));
			return;
		case 'next_only_filter':
			setNextOnlyFilter(_asBool(value, key));
			return;
		case 'hide_todo_settings':
			setHideTodoSettings(_asBool(value, key));
			return;
		case 'technically_working_animate':
			setTechnicallyWorkingAnimate(_asBool(value, key));
			return;
		case 'jog_radial_waveform':
			setJogRadialWaveform(_asBool(value, key));
			return;
		case 'deck_layout': {
			if (value !== 'more' && value !== 'less') {
				throw new Error(`deck_layout must be more|less, got ${String(value)}`);
			}
			setDeckLayoutMode(value as DeckLayoutMode);
			return;
		}
		case 'deck_layout_animate':
			setDeckLayoutAnimate(_asBool(value, key));
			return;
		case 'deck_layout_duration_ms': {
			const n = Number(value);
			if (!(DECK_LAYOUT_DURATIONS_MS as readonly number[]).includes(n)) {
				throw new Error(
					`deck_layout_duration_ms must be one of ${DECK_LAYOUT_DURATIONS_MS.join(', ')}, got ${String(value)}`
				);
			}
			setDeckLayoutDurationMs(n as DeckLayoutDurationMs);
			return;
		}
		case 'auto_sync.rekordbox':
		case 'auto_sync.djay':
		case 'auto_sync.open_dj': {
			const dest = key.slice('auto_sync.'.length) as AutoSyncDestination;
			setAutoSyncDestination(dest, _asBool(value, key));
			return;
		}
		case 'confirm.delete_playlist':
			setConfirmPref('delete_playlist', _asBool(value, key));
			return;
		case 'confirm.dblclick_load_play':
			setConfirmPref('dblclick_load_play', _asBool(value, key));
			return;
		case 'wheel_sensitivity.mouse':
		case 'wheel_sensitivity.trackpad': {
			// setWheelSensitivity owns the range check and throws RangeError
			// outside (0, WHEEL_SENSITIVITY_MAX]; this only has to turn the
			// control's string into a number, or refuse loudly.
			const kind = key.slice('wheel_sensitivity.'.length) as WheelInputKind;
			setWheelSensitivity(kind, _asFactor(value, key));
			return;
		}
	}
}

function _asFactor(value: SettingValue, key: string): number {
	if (typeof value !== 'string' || value.trim() === '') {
		throw new Error(`${key} expects a number, got ${String(value)}`);
	}
	const parsed = Number(value);
	if (!Number.isFinite(parsed)) {
		throw new Error(`${key} expects a number, got ${String(value)}`);
	}
	return parsed;
}

function _asBool(value: SettingValue, key: string): boolean {
	if (typeof value === 'boolean') return value;
	if (value === 'true' || value === 'on' || value === '1') return true;
	if (value === 'false' || value === 'off' || value === '0') return false;
	throw new Error(`${key} expects a boolean, got ${String(value)}`);
}
