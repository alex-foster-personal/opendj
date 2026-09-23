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
	setPreviewBeatSync,
	setConfirmPref,
	setDeckLayoutAnimate,
	setDeckLayoutDurationMs,
	setDeckLayoutMode,
	setHideBrokenLinks,
	setHideTodoSettings,
	setJogRadialWaveform,
	setShowStems,
	setLibraryDensity,
	setLyricsDeckLine,
	setLyricsGlobal,
	setLyricsHoverScrub,
	setLyricsLibraryCol,
	setLyricsLoadStrategy,
	setLyricsWaveformOverlay,
	setNextOnlyFilter,
	setAppPosture,
	setPerfTier,
	setRemixesFilter,
	setTechnicallyWorkingAnimate,
	setTheme,
	setVocalsFilter,
	uiPrefs,
	type AutoSyncDestination,
	type DeckLayoutDurationMs,
	type DeckLayoutMode,
	type LibraryDensity,
	type LyricsLoadStrategy,
	type AppPosturePref,
	type PerfTierPref,
	type PreviewBeatSync,
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
	'preview_beat_sync',
	'auto_play_enabled',
	'auto_play_enforce_order',
	'auto_play_maximize_reach',
	'next_only_filter',
	'remixes_filter',
	'vocals_filter',
	'lyrics_global',
	'lyrics_library_col',
	'lyrics_hover_scrub',
	'lyrics_load_strategy',
	'lyrics_waveform_overlay',
	'lyrics_deck_line',
	'hide_todo_settings',
	'technically_working_animate',
	'jog_radial_waveform',
	'show_stems',
	'deck_layout',
	'deck_layout_animate',
	'deck_layout_duration_ms',
	'auto_sync.rekordbox',
	'auto_sync.djay',
	'auto_sync.open_dj',
	'confirm.delete_playlist',
	'confirm.dblclick_load_play',
	'wheel_sensitivity.mouse',
	'wheel_sensitivity.trackpad',
	'perf_tier',
	'app_posture'
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
		case 'remixes_filter':
			return uiPrefs.remixes_filter;
		case 'vocals_filter':
			return uiPrefs.vocals_filter;
		case 'lyrics_global':
			return uiPrefs.lyrics_global;
		case 'lyrics_library_col':
			return uiPrefs.lyrics_library_col;
		case 'lyrics_hover_scrub':
			return uiPrefs.lyrics_hover_scrub;
		case 'lyrics_load_strategy':
			return uiPrefs.lyrics_load_strategy;
		case 'lyrics_waveform_overlay':
			return uiPrefs.lyrics_waveform_overlay;
		case 'lyrics_deck_line':
			return uiPrefs.lyrics_deck_line;
		case 'hide_todo_settings':
			return uiPrefs.hide_todo_settings;
		case 'technically_working_animate':
			return uiPrefs.technically_working_animate;
		case 'jog_radial_waveform':
			return uiPrefs.jog_radial_waveform;
		case 'show_stems':
			return uiPrefs.show_stems;
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
		case 'preview_beat_sync':
			return uiPrefs.preview_beat_sync;
		case 'perf_tier':
			return uiPrefs.perf_tier;
		case 'app_posture':
			return uiPrefs.app_posture;
		default: {
			const _exhaustive: never = key;
			throw new Error(`Unhandled setting key: ${_exhaustive}`);
		}
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
		case 'remixes_filter':
			setRemixesFilter(_asBool(value, key));
			return;
		case 'vocals_filter':
			setVocalsFilter(_asBool(value, key));
			return;
		case 'lyrics_global':
			setLyricsGlobal(_asBool(value, key));
			return;
		case 'lyrics_library_col':
			setLyricsLibraryCol(_asBool(value, key));
			return;
		case 'lyrics_hover_scrub':
			setLyricsHoverScrub(_asBool(value, key));
			return;
		case 'lyrics_load_strategy': {
			if (value !== 'in-view' && value !== 'hover' && value !== 'off') {
				throw new Error(`lyrics_load_strategy must be in-view|hover|off, got ${String(value)}`);
			}
			setLyricsLoadStrategy(value as LyricsLoadStrategy);
			return;
		}
		case 'lyrics_waveform_overlay':
			setLyricsWaveformOverlay(_asBool(value, key));
			return;
		case 'lyrics_deck_line':
			setLyricsDeckLine(_asBool(value, key));
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
		case 'show_stems':
			setShowStems(_asBool(value, key));
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
		case 'preview_beat_sync': {
			if (value !== 'off' && value !== 'tempo') {
				throw new Error(`preview_beat_sync must be off|tempo, got ${String(value)}`);
			}
			setPreviewBeatSync(value as PreviewBeatSync);
			return;
		}
		case 'perf_tier': {
			if (
				value !== 'auto' &&
				value !== 'low' &&
				value !== 'standard' &&
				value !== 'high'
			) {
				throw new Error(`perf_tier must be auto|low|standard|high, got ${String(value)}`);
			}
			setPerfTier(value as PerfTierPref);
			return;
		}
		case 'app_posture': {
			if (value !== 'prep' && value !== 'gig') {
				throw new Error(`app_posture must be prep|gig, got ${String(value)}`);
			}
			setAppPosture(value as AppPosturePref);
			return;
		}
		default: {
			const _exhaustive: never = key;
			throw new Error(`Unhandled setting key: ${_exhaustive}`);
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
