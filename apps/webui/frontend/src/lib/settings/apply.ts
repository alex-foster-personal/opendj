/**
 * Allowlisted setting mutators. Used by the overlay controls and AI apply.
 * Unknown keys throw (fail-loud).
 */
import { parseWaveformDesignPref } from '$lib/rb/waveform-design';
import { reportSettingSaveError } from '$lib/settings/setting-save-errors';
import { artThumbnailsAvailable, setSidebarArt, sidebarArt } from '$lib/rb/library-sidebar-art.svelte';
import {
	compatibleFilterSettingPatch,
	readCompatibleFilterSetting,
	type CompatibleFilterSettingKey
} from '$lib/settings/compatible-filter-setting';
import { parseWavePalettePref } from '$lib/rb/wave-palette';
import { UI_SKIN_CHOICES, parseUiSkin, parseWaveSplitMaster } from '$lib/rb/ui-skin';
import { parseWavePlayheadMode, parseWaveZoom } from '$lib/rb/wave-zoom';
import { parseSpeakerAutoRecover, SPEAKER_AUTO_RECOVER_PREFS } from '$lib/rb/speaker-link-prefs';
import { parseTempoLaneScale } from '$lib/rb/tempo-lane-prefs';
import {
	DECK_LAYOUT_DURATIONS_MS,
	setAutoPlayEnabled,
	setAutoPlayEnforceOrder,
	setAutoPlayMaximizeReach,
	setAutoSyncDestination,
	setBeatSyncMax,
	setDeckDefaultQuantize,
	setDeckNeedleLock,
	setPreviewBeatSync,
	clearConfirmPref,
	setConfirmPref,
	setDeckLayoutAnimate,
	setDeckLayoutDurationMs,
	setDeckLayoutMode,
	setDeckRightMirror,
	setDeckLeftMirror,
	setPlaylistTreeView,
	setCrossfadeCurve,
	setHideBrokenLinks,
	setHideTodoSettings,
	setHorizontalWheelKnob,
	setDragSensitivity,
	setJogRadialWaveform,
	setShowDevUi,
	setPinVisibleDays,
	setShowStems,
	setWaveEqReflect,
	setMixerStems,
	setDeckHoverLink,
	setShowSections,
	setDeckNumberArt,
	requireDeckNumberArt,
	setSpeakerAutoRecover,
	setShowTempoLane,
	setTempoLaneScale,
	setWaveformDesign,
	setWavePalette,
	setLayoutOverride,
	setUiSkin,
	setWaveSplitMaster,
	setWaveZoom,
	setWaveZoomLinked,
	setWaveZoomPlayhead,
	setLibraryDensity,
	setKeyNotation,
	setLyricsDeckLine,
	setLyricsGlobal,
	setLyricsHoverScrub,
	setLyricsLibraryCol,
	setLyricsLoadStrategy,
	setLyricsWaveformOverlay,
	setNextOnlyFilter,
	setAvailableOfflineFilter,
	setAppPosture,
	setGigHelper,
	setPerfTier,
	setRemixesFilter,
	setTechnicallyWorkingAnimate,
	setTheme,
	setVocalsFilter,
	uiPrefs,
	patchCompatibleFilter,
	type AutoSyncDestination,
	type DeckLayoutDurationMs,
	type DeckLayoutMode,
	type LibraryDensity,
	type LyricsLoadStrategy,
	type AppPosturePref,
	type GigHelperPref,
	type PerfTierPref,
	type PreviewBeatSync,
	type UiTheme
} from '$lib/rb/prefs.svelte';
import { isKeyNotation, KEY_NOTATIONS } from '$lib/rb/key-notation';
import {
	setStoredEngineChoice,
	storedEngineChoice,
	type EngineChoice
} from '$lib/audio-engine/rust-mode.svelte';
import {
	setWheelSensitivity,
	wheelSensitivity,
	type WheelInputKind
} from '$lib/rb/wheel-adjust';
// The leaf, not midi-ui-state: this module is on the library page's first-paint
// path (SettingsOverlay), and midi-ui-state statically pulls in the WebMIDI
// runtime, action glue and every device map (about 27 KB minified) that only
// /performance otherwise loads. See the rb.midi_enabled case below.
import {
	midiEnabledPersisted,
	persistMidiEnabled
} from '$lib/components/rb/midi/midi-enabled-choice';
import {
	featureIconSingleClick,
	setFeatureIconSingleClick
} from '$lib/rb/feature-icon-cluster.svelte';
import { syncDiskPrefs } from '$lib/rb/prefs-hydrate';
import { autoGainPrefs, autoGainPrefsUnavailable, saveAutoGainPrefs } from '$lib/rb/auto-gain.svelte';
import { AUTO_GAIN_TARGET_MAX_LUFS, AUTO_GAIN_TARGET_MIN_LUFS } from '$lib/rb/auto-gain';
import { dropModePrefFromSetting, dropModeSettingValue } from './confirm-drop-mode';
import { isVariantSettingId, type VariantSettingId } from '$lib/settings/variant-setting';
import { readVariant, setVariant } from '$lib/rb/prefs.svelte';

// How a MIDI runtime load failure reaches the user. app-init wires the error
// toast in at boot (toastMidiLoadFailure), so this first-paint module does not
// import stores.svelte, the app's highest fan-in module (quality gate
// frontend.max_fan_in). Until then the failure is still logged, never dropped.
const _logMidiLoadFailure = (exc: unknown): void => console.error('[midi] MIDI could not load', exc);
let _reportMidiLoadFailure = _logMidiLoadFailure;

/** null restores the log-only default (app-init's teardown). */
export function setMidiLoadFailureReporter(report: ((exc: unknown) => void) | null): void {
	_reportMidiLoadFailure = report ?? _logMidiLoadFailure;
}

/** The rb.midi_enabled runtime half lives in midi-ui-state, which is loaded on
 * demand here rather than charging the MIDI runtime to first paint. It persists
 * the choice (localStorage + PUT /api/v1/ui-prefs) and then acts on it: enable
 * requests WebMIDI access, disable detaches the glue and input listeners.
 * Destructured so knip still sees which export is used. */
async function _applyMidiEnabledChoice(enabled: boolean): Promise<void> {
	try {
		const { applyMidiEnabledSetting } = await import('$lib/components/rb/midi/midi-ui-state.svelte');
		await applyMidiEnabledSetting(enabled);
	} catch (exc: unknown) {
		// Nothing acted on the saved choice, so it must not read "on": restore
		// off (persistMidiEnabled bumps the tick the toggle reads) and say why.
		// The disk half too: its usual writer lives in the module that failed
		// to load, and a disk-backed "on" left behind would be hydrated back on
		// the next page load, undoing an "off" the user just chose.
		persistMidiEnabled(false);
		void syncDiskPrefs({ midi_enabled: false });
		_reportMidiLoadFailure(exc);
	}
}

export const ALLOWED_SETTING_KEYS = [
	'theme',
	'hide_broken_links',
	'library_sidebar_art',
	'library_density',
	'key_notation',
	'beat_sync_max',
	'deck_default_quantize',
	'deck_needle_lock',
	'preview_beat_sync',
	'auto_play_enabled',
	'auto_play_enforce_order',
	'auto_play_maximize_reach',
	'next_only_filter',
	'compatible_filter.mode',
	'compatible_filter.mashup_relative',
	'compatible_filter.key_shift_max',
	'remixes_filter',
	'vocals_filter',
	'available_offline_filter',
	'lyrics_global',
	'lyrics_library_col',
	'lyrics_hover_scrub',
	'lyrics_load_strategy',
	'lyrics_waveform_overlay',
	'lyrics_deck_line',
	'hide_todo_settings',
	'show_dev_ui',
	'pin_visible_days',
	'technically_working_animate',
	'jog_radial_waveform',
	'show_stems',
	'deck_hover_link',
	'show_sections',
	'deck_number_art',
	'speaker_auto_recover',
	'show_tempo_lane',
	'tempo_lane_scale',
	'waveform_design',
	'wave_eq_reflect',
	'wave_palette',
	'ui_skin',
	'wave_split_master',
	'wave_zoom',
	'wave_zoom_playhead',
	'wave_zoom_linked',
	'deck_layout',
	'mixer_stems',
	'deck_layout_animate',
	'deck_layout_duration_ms',
	'deck_right_mirror',
	'deck_left_mirror',
	'feature_icon_single_click',
	'playlist_tree_view',
	'auto_sync.rekordbox',
	'auto_sync.djay',
	'auto_sync.open_dj',
	'confirm.delete_playlist',
	'confirm.dblclick_load_play',
	'confirm.playlist_drop_mode',
	'wheel_sensitivity.mouse',
	'wheel_sensitivity.trackpad',
	'drag_sensitivity.base',
	'drag_sensitivity.falloff_px',
	'layout_override.performance',
	'layout_override.library',
	'crossfade_curve',
	'horizontal_wheel_knob',
	'perf_tier',
	'app_posture',
	'rb.midi_enabled',
	'auto_gain.enabled',
	'auto_gain.target_lufs',
	'gig_helper',
	'audio_engine'
] as const;

export type AllowedSettingKey = (typeof ALLOWED_SETTING_KEYS)[number];

export function isAllowedSettingKey(key: string): key is AllowedSettingKey {
	return (ALLOWED_SETTING_KEYS as readonly string[]).includes(key);
}

export function isKnownSettingKey(key: string): key is AllowedSettingKey | VariantSettingId {
	return isAllowedSettingKey(key) || isVariantSettingId(key);
}

export type SettingValue = boolean | string;

/** Why a setting's shown value is not the saved one right now, or null.
 * The overlay prints it under the row instead of passing a fallback off as read. */
export function settingUnavailable(key: string): string | null {
	if (key === 'library_sidebar_art') {
		return artThumbnailsAvailable() ? null : 'Playlist art is turned off by the library.art_thumbnails flag.';
	}
	if (key !== 'auto_gain.enabled' && key !== 'auto_gain.target_lufs') return null;
	const why = autoGainPrefsUnavailable();
	return why === null ? null : `Auto gain settings ${why}.`;
}

function _unhandledSettingKey(key: never): never {
	throw new Error(`Unhandled setting key: ${key}`);
}

const _LIBRARY_UI_PREF_KEYS = [
	'theme',
	'hide_broken_links',
	'library_density',
	'key_notation',
	'beat_sync_max',
	'deck_default_quantize',
	'deck_needle_lock',
	'preview_beat_sync',
	'auto_play_enabled',
	'auto_play_enforce_order',
	'auto_play_maximize_reach',
	'next_only_filter',
	'remixes_filter',
	'vocals_filter',
	'available_offline_filter',
	'lyrics_global',
	'lyrics_library_col',
	'lyrics_hover_scrub',
	'lyrics_load_strategy',
	'lyrics_waveform_overlay',
	'lyrics_deck_line',
	'hide_todo_settings',
	'show_dev_ui',
	'technically_working_animate'
] as const satisfies readonly AllowedSettingKey[];

type _LibraryUiPrefKey = (typeof _LIBRARY_UI_PREF_KEYS)[number];

function _isLibraryUiPrefKey(key: AllowedSettingKey): key is _LibraryUiPrefKey {
	return (_LIBRARY_UI_PREF_KEYS as readonly string[]).includes(key);
}

function _readLibraryUiPref(key: _LibraryUiPrefKey): SettingValue {
	switch (key) {
		case 'theme':
			return uiPrefs.theme;
		case 'hide_broken_links':
			return uiPrefs.hide_broken_links;
		case 'library_density':
			return uiPrefs.library_density;
		case 'key_notation':
			return uiPrefs.key_notation;
		case 'beat_sync_max':
			return uiPrefs.beat_sync_max;
		case 'deck_default_quantize':
			return uiPrefs.deck_default_quantize;
		case 'deck_needle_lock':
			return uiPrefs.deck_needle_lock;
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
		case 'available_offline_filter':
			return uiPrefs.available_offline_filter;
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
		case 'show_dev_ui':
			return uiPrefs.show_dev_ui;
		case 'technically_working_animate':
			return uiPrefs.technically_working_animate;
		case 'preview_beat_sync':
			return uiPrefs.preview_beat_sync;
		default: {
			const _exhaustive: never = key;
			return _unhandledSettingKey(_exhaustive);
		}
	}
}

const _APPEARANCE_AND_DECK_UI_PREF_KEYS = [
	'jog_radial_waveform',
	'show_stems',
	'deck_hover_link',
	'show_sections',
	'deck_number_art',
	'speaker_auto_recover',
	'show_tempo_lane',
	'tempo_lane_scale',
	'waveform_design',
	'wave_eq_reflect',
	'wave_palette',
	'ui_skin',
	'wave_split_master',
	'wave_zoom',
	'wave_zoom_playhead',
	'wave_zoom_linked',
	'deck_layout',
	'mixer_stems',
	'deck_layout_animate',
	'deck_right_mirror',
	'deck_left_mirror',
	'playlist_tree_view',
	'auto_sync.rekordbox',
	'auto_sync.djay',
	'auto_sync.open_dj',
	'crossfade_curve',
	'horizontal_wheel_knob',
	'perf_tier',
	'app_posture',
	'gig_helper'
] as const satisfies readonly AllowedSettingKey[];

type _AppearanceAndDeckUiPrefKey = (typeof _APPEARANCE_AND_DECK_UI_PREF_KEYS)[number];

function _isAppearanceAndDeckUiPrefKey(key: AllowedSettingKey): key is _AppearanceAndDeckUiPrefKey {
	return (_APPEARANCE_AND_DECK_UI_PREF_KEYS as readonly string[]).includes(key);
}

function _readAppearanceAndDeckUiPref(key: _AppearanceAndDeckUiPrefKey): SettingValue {
	switch (key) {
		case 'jog_radial_waveform':
			return uiPrefs.jog_radial_waveform;
		case 'show_stems':
			return uiPrefs.show_stems;
		case 'deck_hover_link':
			return uiPrefs.deck_hover_link;
		case 'show_sections':
			return uiPrefs.show_sections;
		case 'deck_number_art':
			return uiPrefs.deck_number_art;
		case 'speaker_auto_recover':
			return uiPrefs.speaker_auto_recover;
		case 'show_tempo_lane':
			return uiPrefs.show_tempo_lane;
		case 'tempo_lane_scale':
			return uiPrefs.tempo_lane_scale;
		case 'waveform_design':
			return uiPrefs.waveform_design;
		case 'wave_eq_reflect':
			return uiPrefs.wave_eq_reflect;
		case 'wave_palette':
			return uiPrefs.wave_palette;
		case 'ui_skin':
			return uiPrefs.ui_skin;
		case 'wave_split_master':
			return uiPrefs.wave_split_master;
		case 'wave_zoom':
			return uiPrefs.wave_zoom;
		case 'wave_zoom_playhead':
			return uiPrefs.wave_zoom_playhead;
		case 'wave_zoom_linked':
			return uiPrefs.wave_zoom_linked;
		case 'deck_layout':
			return uiPrefs.deck_layout;
		case 'mixer_stems':
			return uiPrefs.mixer_stems;
		case 'deck_layout_animate':
			return uiPrefs.deck_layout_animate;
		case 'deck_right_mirror':
			return uiPrefs.deck_right_mirror;
		case 'deck_left_mirror':
			return uiPrefs.deck_left_mirror;
		case 'playlist_tree_view':
			return uiPrefs.playlist_tree_view;
		case 'auto_sync.rekordbox':
			return uiPrefs.auto_sync.rekordbox;
		case 'auto_sync.djay':
			return uiPrefs.auto_sync.djay;
		case 'auto_sync.open_dj':
			return uiPrefs.auto_sync.open_dj;
		case 'crossfade_curve':
			return uiPrefs.crossfade_curve;
		case 'horizontal_wheel_knob':
			return uiPrefs.horizontal_wheel_knob;
		case 'perf_tier':
			return uiPrefs.perf_tier;
		case 'app_posture':
			return uiPrefs.app_posture;
		case 'gig_helper':
			return uiPrefs.gig_helper;
		default: {
			const _exhaustive: never = key;
			return _unhandledSettingKey(_exhaustive);
		}
	}
}

const _CONFIRM_SETTING_KEYS = [
	'confirm.delete_playlist',
	'confirm.dblclick_load_play',
	'confirm.playlist_drop_mode'
] as const satisfies readonly AllowedSettingKey[];

type _ConfirmSettingKey = (typeof _CONFIRM_SETTING_KEYS)[number];

function _isConfirmSettingKey(key: AllowedSettingKey): key is _ConfirmSettingKey {
	return (_CONFIRM_SETTING_KEYS as readonly string[]).includes(key);
}

function _readConfirmSetting(key: _ConfirmSettingKey): SettingValue {
	switch (key) {
		case 'confirm.delete_playlist':
			return uiPrefs.confirm.delete_playlist !== false;
		case 'confirm.dblclick_load_play':
			return uiPrefs.confirm.dblclick_load_play !== false;
		case 'confirm.playlist_drop_mode':
			return dropModeSettingValue(uiPrefs.confirm.playlist_drop_mode);
		default: {
			const _exhaustive: never = key;
			return _unhandledSettingKey(_exhaustive);
		}
	}
}

type _NumericAndExternalSettingKey = Exclude<
	AllowedSettingKey,
	_LibraryUiPrefKey | _AppearanceAndDeckUiPrefKey | _ConfirmSettingKey
>;

function _readNumericAndExternalSetting(key: _NumericAndExternalSettingKey): SettingValue {
	switch (key) {
		case 'library_sidebar_art':
			return sidebarArt.pref;
		case 'compatible_filter.mode':
		case 'compatible_filter.mashup_relative':
		case 'compatible_filter.key_shift_max':
			return readCompatibleFilterSetting(uiPrefs.compatible_filter, key);
		case 'pin_visible_days':
			return String(uiPrefs.pin_visible_days);
		case 'deck_layout_duration_ms':
			return String(uiPrefs.deck_layout_duration_ms);
		case 'feature_icon_single_click':
			return featureIconSingleClick();
		case 'wheel_sensitivity.mouse':
			return String(wheelSensitivity().mouse);
		case 'wheel_sensitivity.trackpad':
			return String(wheelSensitivity().trackpad);
		case 'drag_sensitivity.base':
			return String(uiPrefs.drag_sensitivity.base);
		case 'drag_sensitivity.falloff_px':
			return String(uiPrefs.drag_sensitivity.falloff_px);
		case 'layout_override.performance':
			return uiPrefs.layout_override.performance;
		case 'layout_override.library':
			return uiPrefs.layout_override.library;
		case 'rb.midi_enabled':
			return midiEnabledPersisted();
		case 'auto_gain.enabled':
			return autoGainPrefs.enabled;
		case 'auto_gain.target_lufs':
			return String(autoGainPrefs.target_lufs);
		case 'audio_engine':
			return storedEngineChoice();
		default: {
			const _exhaustive: never = key;
			return _unhandledSettingKey(_exhaustive);
		}
	}
}

export function readSettingValue(key: AllowedSettingKey | VariantSettingId): SettingValue {
	if (isVariantSettingId(key)) return readVariant(key);
	const allowed = key as AllowedSettingKey;
	if (_isLibraryUiPrefKey(allowed)) return _readLibraryUiPref(allowed);
	if (_isAppearanceAndDeckUiPrefKey(allowed)) return _readAppearanceAndDeckUiPref(allowed);
	if (_isConfirmSettingKey(allowed)) return _readConfirmSetting(allowed);
	return _readNumericAndExternalSetting(allowed);
}

/** KEYSHIFT-01..05: validates synchronously (a bad value throws at the call),
 * then saves through the verified disk write. */
function _applyCompatibleFilterSetting(key: CompatibleFilterSettingKey, value: boolean | string): void {
	const patch = compatibleFilterSettingPatch(key, value);
	patchCompatibleFilter(patch).catch((err: unknown) =>
		reportSettingSaveError(`Compatible filter setting not saved: ${err}`, err)
	);
}

export function applySettingChange(key: string, value: SettingValue): void {
	if (isVariantSettingId(key)) {
		if (typeof value !== 'string') {
			throw new Error(`${key} must be a string variant value, got ${String(value)}`);
		}
		setVariant(key, value);
		return;
	}
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
		case 'library_sidebar_art':
			void setSidebarArt(_asBool(value, key)).catch((exc: unknown) => reportSettingSaveError(`Playlist art setting not saved: ${String(exc)}`, exc));
			return;
		case 'library_density': {
			if (value !== 'compact' && value !== 'cosy') {
				throw new Error(`library_density must be compact|cosy, got ${String(value)}`);
			}
			setLibraryDensity(value as LibraryDensity);
			return;
		}
		case 'key_notation': {
			if (!isKeyNotation(value)) {
				throw new Error(`key_notation must be ${KEY_NOTATIONS.join('|')}, got ${String(value)}`);
			}
			setKeyNotation(value);
			return;
		}
		case 'beat_sync_max':
			setBeatSyncMax(_asBool(value, key));
			return;
		case 'deck_default_quantize':
			setDeckDefaultQuantize(_asBool(value, key));
			return;
		case 'deck_needle_lock':
			setDeckNeedleLock(_asBool(value, key));
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
		case 'compatible_filter.mashup_relative':
			_applyCompatibleFilterSetting(key, _asBool(value, key));
			return;
		case 'compatible_filter.mode':
		case 'compatible_filter.key_shift_max':
			_applyCompatibleFilterSetting(key, value);
			return;
		case 'remixes_filter':
			setRemixesFilter(_asBool(value, key));
			return;
		case 'vocals_filter':
			setVocalsFilter(_asBool(value, key));
			return;
		case 'available_offline_filter':
			setAvailableOfflineFilter(_asBool(value, key));
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
		case 'show_dev_ui':
			setShowDevUi(_asBool(value, key));
			return;
		case 'pin_visible_days':
			setPinVisibleDays(_asFactor(value, key));
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
		case 'wave_eq_reflect':
			setWaveEqReflect(_asBool(value, key));
			return;
		case 'deck_hover_link':
			setDeckHoverLink(_asBool(value, key));
			return;
		case 'show_sections':
			setShowSections(_asBool(value, key));
			return;
		case 'deck_number_art':
			setDeckNumberArt(requireDeckNumberArt(value, key));
			return;
		case 'speaker_auto_recover': {
			const pref = parseSpeakerAutoRecover(value);
			if (pref === undefined) {
				throw new Error(`speaker_auto_recover must be ${SPEAKER_AUTO_RECOVER_PREFS.join('|')}, got ${String(value)}`);
			}
			setSpeakerAutoRecover(pref);
			return;
		}
		case 'show_tempo_lane':
			setShowTempoLane(_asBool(value, key));
			return;
		case 'tempo_lane_scale': {
			const scale = parseTempoLaneScale(value);
			if (scale === undefined) throw new Error(`tempo_lane_scale must be fixed_10|relative, got ${String(value)}`);
			setTempoLaneScale(scale);
			return;
		}
		case 'waveform_design': {
			const design = parseWaveformDesignPref(value);
			if (design === undefined) {
				throw new Error(`waveform_design must be auto|tri-band|mono|line|blocks, got ${String(value)}`);
			}
			setWaveformDesign(design);
			return;
		}
		case 'wave_palette': {
			const choice = parseWavePalettePref(value);
			if (choice === undefined) {
				throw new Error(`wave_palette must be auto|rekordbox|legacy|mono, got ${String(value)}`);
			}
			setWavePalette(choice);
			return;
		}
		case 'ui_skin': {
			const skin = parseUiSkin(value);
			if (skin === undefined) throw new Error(`ui_skin must be ${UI_SKIN_CHOICES.join('|')}, got ${String(value)}`);
			setUiSkin(skin);
			return;
		}
		case 'wave_zoom': {
			const zoom = parseWaveZoom(value);
			if (zoom === undefined) throw new Error(`wave_zoom must be 4|8|16|32|64|whole, got ${String(value)}`);
			setWaveZoom(zoom);
			return;
		}
		case 'wave_zoom_playhead': {
			const mode = parseWavePlayheadMode(value);
			if (mode === undefined) throw new Error(`wave_zoom_playhead must be elastic|center, got ${String(value)}`);
			setWaveZoomPlayhead(mode);
			return;
		}
		case 'wave_zoom_linked':
			setWaveZoomLinked(_asBool(value, key));
			return;
		case 'wave_split_master': {
			const mode = parseWaveSplitMaster(value);
			if (mode === undefined) throw new Error(`wave_split_master must be auto|on|off, got ${String(value)}`);
			setWaveSplitMaster(mode);
			return;
		}
		case 'deck_layout': {
			if (value !== 'more' && value !== 'less') {
				throw new Error(`deck_layout must be more|less, got ${String(value)}`);
			}
			setDeckLayoutMode(value as DeckLayoutMode);
			return;
		}
		case 'mixer_stems':
			setMixerStems(_asBool(value, key));
			return;
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
		case 'deck_right_mirror':
			setDeckRightMirror(_asBool(value, key));
			return;
		case 'deck_left_mirror':
			setDeckLeftMirror(_asBool(value, key));
			return;
		case 'feature_icon_single_click':
			setFeatureIconSingleClick(_asBool(value, key));
			return;
		case 'playlist_tree_view': {
			if (value !== 'tree' && value !== 'column') {
				throw new Error(`playlist_tree_view must be tree|column, got ${String(value)}`);
			}
			setPlaylistTreeView(value);
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
		case 'confirm.playlist_drop_mode':
			if (value === 'ask') {
				// Verified (PR #4014, Sol P1): the reset commits only after the disk
				// delete lands, and a failure is shown rather than left for the next
				// hydration to quietly restore Add or Move.
				clearConfirmPref('playlist_drop_mode').catch((err: unknown) =>
					reportSettingSaveError(`Could not reset playlist drop to Ask: ${err}`, err)
				);
			} else {
				const remembered = dropModePrefFromSetting(value);
				if (remembered === undefined) {
					throw new Error(`confirm.playlist_drop_mode must be ask|add|move, got ${String(value)}`);
				}
				setConfirmPref('playlist_drop_mode', remembered);
			}
			return;
		case 'auto_gain.enabled':
			void saveAutoGainPrefs({ enabled: _asBool(value, key) });
			return;
		case 'auto_gain.target_lufs': {
			const target = _asFactor(value, key);
			if (target < AUTO_GAIN_TARGET_MIN_LUFS || target > AUTO_GAIN_TARGET_MAX_LUFS) {
				throw new RangeError(`${key} must be ${AUTO_GAIN_TARGET_MIN_LUFS}..${AUTO_GAIN_TARGET_MAX_LUFS} LUFS, got ${target}`);
			}
			void saveAutoGainPrefs({ target_lufs: target });
			return;
		}
		case 'wheel_sensitivity.mouse':
		case 'wheel_sensitivity.trackpad': {
			// setWheelSensitivity owns the range check and throws RangeError
			// outside (0, WHEEL_SENSITIVITY_MAX]; this only has to turn the
			// control's string into a number, or refuse loudly.
			const kind = key.slice('wheel_sensitivity.'.length) as WheelInputKind;
			setWheelSensitivity(kind, _asFactor(value, key));
			return;
		}
		case 'drag_sensitivity.base':
			setDragSensitivity({ base: _asFactor(value, key) });
			return;
		case 'drag_sensitivity.falloff_px':
			setDragSensitivity({ falloff_px: _asFactor(value, key) });
			return;
		case 'layout_override.performance':
		case 'layout_override.library': {
			if (typeof value !== 'string') {
				throw new Error(`${key} must be a string preset, got ${String(value)}`);
			}
			const surface = key === 'layout_override.performance' ? 'performance' : 'library';
			setLayoutOverride(surface, value);
			return;
		}
		case 'crossfade_curve': {
			if (value !== 'magic') {
				throw new Error(`crossfade_curve must be magic until curves are built, got ${String(value)}`);
			}
			setCrossfadeCurve('magic');
			return;
		}
		case 'horizontal_wheel_knob': {
			if (value !== 'filter' && value !== 'color') {
				throw new Error(`horizontal_wheel_knob must be filter|color, got ${String(value)}`);
			}
			setHorizontalWheelKnob(value as 'filter' | 'color');
			return;
		}
		case 'preview_beat_sync': {
			if (value !== 'off' && value !== 'tempo' && value !== 'phase') {
				throw new Error(`preview_beat_sync must be off|tempo|phase, got ${String(value)}`);
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
		case 'rb.midi_enabled': {
			const enabled = _asBool(value, key);
			// The local choice lands now, so readSettingValue() reflects it at once.
			persistMidiEnabled(enabled);
			void _applyMidiEnabledChoice(enabled);
			return;
		}
		case 'gig_helper': {
			if (value !== 'unset' && value !== 'off' && value !== 'on') {
				throw new Error(`gig_helper must be unset|off|on, got ${String(value)}`);
			}
			setGigHelper(value as GigHelperPref);
			return;
		}
		case 'audio_engine':
			// Validates, and throws on anything but webaudio|rust.
			setStoredEngineChoice(value as EngineChoice);
			return;
		default: {
			const _exhaustive: never = key;
			return _unhandledSettingKey(_exhaustive);
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
