/**
 * Warp-style settings catalog: working prefs + grayed PARITY-TODO stubs.
 * Search filters this list in-place (results ARE the settings).
 */

import { APP_POSTURE_SETTING } from './app-posture-setting';
import { GIG_HELPER_SETTING } from './gig-helper-setting';
import { AUDIO_ENGINE_SETTING } from './audio-engine-setting';
import { PREVIEW_BEAT_SYNC_SETTING } from './preview-beat-sync-setting';
import { CONFIRMATION_SETTINGS } from './confirmation-settings';
import { WAVE_PALETTE_SETTING } from './wave-palette-setting';
import { DEV_UI_SETTING, HIDE_TODO_SETTING } from './dev-ui-setting';
import {
	WHEEL_SENSITIVITY,
	WHEEL_SENSITIVITY_MAX,
	WHEEL_SENSITIVITY_MIN,
	WHEEL_SENSITIVITY_STEP
} from '$lib/rb/wheel-adjust';

export type { SettingDef, SettingGroupId } from './catalog-types';
import type { SettingControl, SettingDef, SettingGroup, SettingGroupId } from './catalog-types';
import { DJAY_PARITY_STUBS, REKORDBOX_PARITY_STUBS } from './catalog-parity-stubs';

/** Canonical PARITY-TODO stub title. Declared here, not just in
 * catalog-parity-stubs.ts, so the shared-constant drift check (H13 + M20 in
 * inert-controls.test.mjs) still finds it verbatim in this module. Not
 * exported: the test reads this file's own source text, it never imports
 * this constant, and an unused export is its own quality-gate regression. */
const INERT_TITLE = 'not implemented - see PARITY-TODO';

export const SETTING_GROUPS: readonly SettingGroup[] = [
	{ id: 'appearance', label: 'Appearance' },
	{ id: 'library', label: 'Library' },
	{ id: 'performance', label: 'Performance' },
	{ id: 'confirmations', label: 'Confirmations' },
	{ id: 'sync', label: 'Sync' },
	{ id: 'cloudsync', label: 'CloudSync' },
	{ id: 'advanced', label: 'Advanced' },
	{ id: 'rekordbox', label: 'Rekordbox (todo)' },
	{ id: 'djay', label: 'djay Pro (todo)' }
];

/** Working settings first, then catalog stubs for parity browsing. */
export const SETTINGS_CATALOG: readonly SettingDef[] = [
	// ----- working --------------------------------------------------------
	{
		id: 'theme',
		label: 'Theme',
		group: 'appearance',
		keywords: ['dark', 'light', 'chrome', 'appearance', 'mode'],
		title: 'App chrome theme (dark or light)',
		detail: 'Applies to the shell and /performance. Instant; persisted locally and to ui-prefs.',
		implemented: true,
		control: {
			kind: 'enum',
			options: [
				{ value: 'dark', label: 'Dark' },
				{ value: 'light', label: 'Light' }
			]
		}
	},
	{
		id: 'hide_broken_links',
		label: 'Hide broken links',
		group: 'library',
		keywords: ['missing', 'file', 'gray', 'unavailable', 'broken'],
		title: 'Hide tracks whose audio file is missing',
		detail:
			'When on, missing-file rows and empty playlists leave the browser lists. Default off (grayed but visible).',
		implemented: true,
		control: { kind: 'boolean' }
	},
	{
		id: 'library_watcher_folders',
		label: 'Watcher folders (v2)',
		group: 'library',
		keywords: ['watcher', 'folder', 'import', 'auto', 'monitor', 'ingest'],
		title: 'Folder paths to watch for auto-import (v2 - not active yet)',
		detail: 'One absolute path per line, checked to exist on save. No watcher runs until v2.',
		implemented: true,
		control: {
			kind: 'path_lines',
			v2Notice: 'v2 - folder watcher not active; paths are stored for a future release only.'
		}
	},
	{
		id: 'library_density',
		label: 'Library density',
		group: 'library',
		keywords: ['compact', 'cosy', 'cozy', 'row', 'height', 'table'],
		title: 'Track table row height',
		detail: 'Compact keeps tight rows; Cosy adds vertical padding in the browser track list only.',
		implemented: true,
		control: {
			kind: 'enum',
			options: [
				{ value: 'compact', label: 'Compact' },
				{ value: 'cosy', label: 'Cosy' }
			]
		}
	},
	{
		id: 'next_only_filter',
		label: 'Compatible-only library filter',
		group: 'library',
		keywords: ['next', 'camelot', 'bpm', 'tab', 'suggest', 'compatible'],
		title: 'Show only tracks compatible with the reference deck (master, else playing, else any loaded with key and BPM)',
		detail:
			"Shown as the 'compatible' checkbox in the library header. Filters the library list vs the master, else playing, else any loaded with key and BPM (Camelot family, BPM window, half/double folds). Also toggled with Tab on /performance.",
		implemented: true,
		control: { kind: 'boolean' }
	},
	{
		id: 'remixes_filter',
		label: 'Remixes library filter',
		group: 'library',
		keywords: ['remix', 'bootleg', 'rework', 'vip', 'edit', 'version', 'filter'],
		title: 'Keep only remixes (title version markers)',
		detail:
			'Title-marker heuristic (remix/bootleg/rework/VIP/non-radio edit); the lyric repair signal joins it after the full-library alignment run. Checkbox in the library toolbar.',
		implemented: true,
		control: { kind: 'boolean' }
	},
	{
		id: 'vocals_filter',
		label: 'Vocals library filter',
		group: 'library',
		keywords: ['vocals', 'lyrics', 'lines', 'singing', 'filter', 'acapella'],
		title: 'Keep only tracks with >5 lines of lyrics',
		detail:
			'Requires real word-level lyrics from the pipeline; unprocessed tracks are excluded. Checkbox in the library toolbar.',
		implemented: true,
		control: { kind: 'boolean' }
	},
	{
		id: 'available_offline_filter',
		label: 'Available offline library filter',
		group: 'library',
		keywords: ['offline', 'local', 'cloud', 'streaming', 'download', 'filter'],
		title: 'Keep only tracks with local audio present',
		detail:
			'Excludes cloud-only and streaming-only rows. Checkbox in the library header; distinct from the CAT-07 download action.',
		implemented: true,
		control: { kind: 'boolean' }
	},
	{
		id: 'beat_sync_max',
		label: 'Beat Sync Max',
		group: 'performance',
		keywords: ['beatsync', 'bar', 'phase', 'seek', 'sync', 'master'],
		title: 'BAR downbeat lock on every relocate, held over playback',
		detail:
			'When on, synced playing decks keep PQTZ n=1 aligned, including after the lock, until Beat Sync Max or Beat Sync is turned off. When off, followers sync on seek using their own BEAT/BAR mode; the master free-seeks.',
		implemented: true,
		control: { kind: 'boolean' }
	},
	PREVIEW_BEAT_SYNC_SETTING,
	AUDIO_ENGINE_SETTING,
	{
		id: 'perf_tier',
		label: 'Performance tier',
		group: 'performance',
		keywords: ['tier', 'performance', 'machine', 'ram', 'cpu', 'cache', 'scalability'],
		title: 'Machine performance tier (Auto, or an explicit Low/Standard/High)',
		detail:
			'Latency budgets never relax on weak machines; features scale instead. Auto uses the engine psutil host read (never the webview). Low shrinks prefetch and ANLZ caches; High allows more.',
		implemented: true,
		control: {
			kind: 'enum',
			options: [
				{ value: 'auto', label: 'Auto' },
				{ value: 'low', label: 'Low' },
				{ value: 'standard', label: 'Standard' },
				{ value: 'high', label: 'High' }
			]
		}
	},
	APP_POSTURE_SETTING,
	GIG_HELPER_SETTING,
	{
		id: 'auto_play_enabled',
		label: 'AutoPlay',
		group: 'performance',
		keywords: ['autoplay', 'auto', 'next', 'handoff', 'suggest', 'follower'],
		title: 'Auto-load next track onto a free deck near end',
		detail:
			'When on, in the last ~16s of the playing source deck, load the next playlist track onto a free/stopped follower and play (hard-cut). Default pick: earliest unplayed key+-1 within Beat Sync BPM range. Hover AutoPlay for enforce-order.',
		implemented: true,
		control: { kind: 'boolean' }
	},
	{
		id: 'auto_play_enforce_order',
		label: 'AutoPlay enforce play order',
		group: 'performance',
		keywords: ['autoplay', 'order', 'playlist', 'sequential'],
		title: 'AutoPlay walks strict playlist order',
		detail:
			'When on, AutoPlay takes the next membership row after the current track. When off (default), picks the earliest unplayed playlist track with Camelot key +-1 and BPM inside Beat Sync pitch bounds.',
		implemented: true,
		control: { kind: 'boolean' }
	},
	{
		id: 'auto_play_maximize_reach',
		label: 'AutoPlay maximize reach',
		group: 'performance',
		keywords: ['autoplay', 'reach', 'slack', 'path', 'strand'],
		title: 'AutoPlay prefers fewer-outward candidates to avoid stranding',
		detail:
			'Smart mode only (ignored when enforce play order is on). When on (default), among key+-1 / BPM-compatible next tracks, pick the one with the fewest onward options so later tracks stay reachable. Cap ~50k compatibility checks per pick; over budget falls back to earliest.',
		implemented: true,
		control: { kind: 'boolean' }
	},
	{
		id: 'technically_working_animate',
		label: 'Technically-working mode animation',
		group: 'performance',
		keywords: ['technically', 'working', 'overlay', 'animate', 'fade', 'edge', 'reveal'],
		title: 'Fade regions in/out on edge-reveal (cmd+R overlay mode)',
		detail:
			'When on (default), revealing/hiding a region in overlay mode cross-fades. Off swaps instantly, no transition.',
		implemented: true,
		control: { kind: 'boolean' }
	},
	{
		id: 'jog_radial_waveform',
		label: 'Jog dial radial waveform',
		group: 'performance',
		keywords: ['radial', 'jog', 'waveform', 'wheel', 'polar', 'dial', 'preview'],
		title: 'Show preview waveform as a polar plot on jog dials',
		detail:
			'When on, each loaded deck paints its 400-point preview waveform radially on the jog wheel face and hides the red position tick. The white progress trail still shows playback position. Default off.',
		implemented: true,
		control: { kind: 'boolean' }
	},
	{
		id: 'show_stems',
		label: 'Stem mini-waveforms',
		group: 'performance',
		keywords: ['stem', 'stems', 'mini', 'waveform', 'wavestack', 'demucs', 'vocals'],
		title: 'Show per-stem mini-waveforms under deck waveforms',
		detail:
			'When on, each loaded deck paints one mini-waveform row per stem control (VOCAL, INST, DRUMS) using server peak envelopes. Default off. Same path as the show_stems performance command.',
		implemented: true,
		control: { kind: 'boolean' }
	},
	{
		id: 'waveform_design',
		label: 'Waveform design',
		group: 'performance',
		keywords: ['waveform', 'design', 'tri-band', 'mono', 'line', 'wavestack', 'strip'],
		title: 'Deck and library waveform paint style',
		detail:
			'Tri-band matches rekordbox-style stacked frequency bands. Mono draws a single envelope. Line draws a stroke outline. The preview below updates when you change the selection.',
		implemented: true,
		control: {
			kind: 'enum',
			options: [
				{ value: 'tri-band', label: 'Tri-band bars' },
				{ value: 'mono', label: 'Mono envelope' },
				{ value: 'line', label: 'Line outline' }
			]
		}
	},
	WAVE_PALETTE_SETTING,
	{
		id: 'deck_layout',
		label: 'Deck layout (MORE/LESS)',
		group: 'performance',
		keywords: ['deck', 'layout', 'more', 'less', '2 deck', '4 deck', 'library', 'space', 'toggle'],
		title: 'Two vs four deck performance view',
		detail:
			'LESS collapses deck 3/4 chrome (mixer strips + waveform rows) and gives the library more room. Decks 3/4 keep playing and stay controllable over IPC - chrome only. Cmd/Ctrl+2 = less, Cmd/Ctrl+4 = more.',
		implemented: true,
		control: {
			kind: 'enum',
			options: [
				{ value: 'more', label: 'More (4 deck)' },
				{ value: 'less', label: 'Less (2 deck)' }
			]
		}
	},
	{
		id: 'deck_layout_animate',
		label: 'Animate deck layout switch',
		group: 'performance',
		keywords: ['deck', 'layout', 'animate', 'transition', 'more', 'less'],
		title: 'Animate the MORE/LESS deck layout switch',
		detail:
			'When on (default), switching MORE/LESS cross-fades and shrinks the collapsing panels. Off swaps instantly. prefers-reduced-motion always forces instant regardless.',
		implemented: true,
		control: { kind: 'boolean' }
	},
	{
		id: 'deck_right_mirror',
		label: 'Mirror deck 2 controls',
		group: 'performance',
		keywords: ['deck', 'mirror', 'symmetry', 'deck 2', 'layout', 'right column'],
		title: 'Mirror deck 2 main control row for mixer-facing symmetry',
		detail:
			'When on, deck 2 control row order is reversed horizontally (block order inside each cluster stays the same). Default off.',
		implemented: true,
		control: { kind: 'boolean' }
	},
	{
		id: 'playlist_tree_view',
		label: 'Playlist sidebar layout',
		group: 'library',
		keywords: ['playlist', 'tree', 'column', 'browser', 'library', 'sidebar'],
		title: 'Playlist sidebar tree vs column browser',
		detail: 'Tree shows the playlist list; column browser shows genre/artist/album columns. Persisted across sessions.',
		implemented: true,
		control: {
			kind: 'enum',
			options: [
				{ value: 'tree', label: 'Tree list' },
				{ value: 'column', label: 'Column browser' }
			]
		}
	},
	{
		id: 'deck_layout_duration_ms',
		label: 'Deck layout switch duration',
		group: 'performance',
		keywords: ['deck', 'layout', 'duration', 'ms', 'speed', 'transition'],
		title: 'MORE/LESS transition duration',
		detail:
			'How long the MORE/LESS deck layout transition takes when animation is on. Default 200ms.',
		implemented: true,
		control: {
			kind: 'enum',
			options: [
				{ value: '0', label: '0ms' },
				{ value: '100', label: '100ms' },
				{ value: '200', label: '200ms' },
				{ value: '300', label: '300ms' },
				{ value: '400', label: '400ms' }
			]
		}
	},
	{
		id: 'wheel_sensitivity.mouse',
		label: 'Wheel sensitivity: mouse',
		group: 'performance',
		keywords: [
			'wheel', 'scroll', 'scroll wheel', 'mouse', 'sensitivity', 'notch',
			'detent', 'dial', 'knob', 'fader', 'crossfader', 'pitch'
		],
		title: 'How far one notched mouse-wheel detent moves a dial or fader (1x = the declared step)',
		detail:
			'Multiplier applied to every wheel-adjustable control: the mixer dials, the channel level faders, the crossfader, the pitch faders, the headphone mix/level, and the shift-selected dial driven by the page-level wheel. 1x is the reference the rest of the app was built around. Persisted locally, so it survives a reload.',
		implemented: true,
		control: _wheelSensitivityControl(WHEEL_SENSITIVITY.mouse)
	},
	{
		id: 'wheel_sensitivity.trackpad',
		label: 'Wheel sensitivity: trackpad',
		group: 'performance',
		keywords: [
			'wheel', 'scroll', 'trackpad', 'touchpad', 'two finger', 'macbook',
			'sensitivity', 'hypersensitive', 'dial', 'knob', 'fader'
		],
		title: 'Multiplier for a trackpad two-finger scroll, which emits many events where a mouse emits one detent',
		detail:
			'A macOS trackpad sends a dense burst of wheel events for a single finger movement, so it is scaled separately from a mouse: 1x here means "as sensitive as the mouse", and the shipped default is 3x less sensitive, derived from WHEEL_TRACKPAD_EVENTS_PER_DETENT in lib/rb/wheel-adjust.ts. Same controls and same persistence as the mouse factor.',
		implemented: true,
		control: _wheelSensitivityControl(WHEEL_SENSITIVITY.trackpad)
	},
	...CONFIRMATION_SETTINGS,
	{
		id: 'auto_sync',
		label: 'Auto-sync destinations',
		group: 'sync',
		keywords: ['rekordbox', 'djay', 'opendj', 'open-dj', 'ratings', 'writeback', 'vendor'],
		title: 'Preferred destinations for rating/metadata writeback',
		detail:
			'Saves which vendors you want auto-sync to target. Manual sync runs through POST /api/v1/rb-djay-sync/* or python -m apps.sync (dry-run by default). Preference + plumbing only.',
		implemented: true,
		control: {
			kind: 'multi_bool',
			keys: [
				{
					id: 'rekordbox',
					label: 'Rekordbox',
					title: 'Prefer rekordbox as an auto-sync destination (writeback still CLI)'
				},
				{
					id: 'djay',
					label: 'djay',
					title: 'Prefer djay as an auto-sync destination (writeback still CLI)'
				},
				{
					id: 'open_dj',
					label: 'OpenDJ',
					title: 'Prefer OpenDJ as an auto-sync destination (writeback not implemented)'
				}
			]
		}
	},
	{
		id: 'sync.run_ratings_writeback',
		label: 'Run ratings writeback now',
		group: 'sync',
		keywords: ['apply_ratings', 'writeback', 'sync', 'now', 'cli'],
		title: 'POST /api/v1/rb-djay-sync/ratings/apply (dry-run default)',
		detail:
			'Ratings sync is reachable at POST /api/v1/rb-djay-sync/ratings/apply and python -m apps.sync apply-ratings. Live writes still require explicit risk acknowledgement and the rekordbox writeback gate.',
		implemented: true,
		control: { kind: 'link', href: '/api/v1/rb-djay-sync/status' }
	},
	{
		id: 'lyrics_global',
		label: 'Lyrics overlays (master)',
		group: 'performance',
		keywords: ['lyrics', 'global', 'master', 'overlay', 'karaoke', 'hide', 'all'],
		title: 'Master switch for every lyric overlay',
		detail:
			'One switch (also the LYR icon, top-left) that hides the waveform word lanes, deck lyric lines and scrub-hover words everywhere at once. Per-surface toggles below keep their state and return when this comes back on. The library Lyrics column is data, not an overlay - it has its own setting.',
		implemented: true,
		control: { kind: 'boolean' }
	},
	{
		id: 'lyrics_library_col',
		label: 'Lyrics column',
		group: 'library',
		keywords: ['lyrics', 'column', 'karaoke', 'words', 'tooltip'],
		title: 'Lyrics column in the library table',
		detail:
			'Adds a Lyrics column: verdict + sync quality at a glance, full text with line breaks and source on hover. Off removes the column and its hover fetches.',
		implemented: true,
		control: { kind: 'boolean' }
	},
	{
		id: 'lyrics_hover_scrub',
		label: 'Lyric scrub on vocal bars',
		group: 'library',
		keywords: ['lyrics', 'scrub', 'hover', 'vocal', 'bars', 'preview'],
		title: 'Word readout while hover-scrubbing a preview strip',
		detail:
			'Hovering the blue vocal bars shows the word under the pointer and lets you click a word to audition from it. Off keeps strips as plain seek bars.',
		implemented: true,
		control: { kind: 'boolean' }
	},
	{
		id: 'lyrics_load_strategy',
		label: 'Lyric loading',
		group: 'library',
		keywords: ['lyrics', 'memory', 'performance', 'load', 'debounce', 'ram'],
		title: 'When word timings are fetched into memory',
		detail:
			'A playlist of word timings is too heavy to preload. In-view loads rows as they scroll in (fastest hovers, most RAM); Hover waits for a 500ms hover (default); Off never loads in the library - deck and stage still load their own.',
		implemented: true,
		control: {
			kind: 'enum',
			options: [
				{ value: 'hover', label: 'On hover (500ms)' },
				{ value: 'in-view', label: 'Rows in view' },
				{ value: 'off', label: 'Off in library' }
			]
		}
	},
	{
		id: 'lyrics_waveform_overlay',
		label: 'Waveform lyrics',
		group: 'performance',
		keywords: ['lyrics', 'waveform', 'overlay', 'deck', 'karaoke', 'lanes'],
		title: 'Word lanes over the main deck waveforms',
		detail:
			'Draws upcoming words over each deck waveform, wrapping to a second lane when words overlap. Per-deck toggle lives on the waveform gutter; this is the global default.',
		implemented: true,
		control: { kind: 'boolean' }
	},
	{
		id: 'lyrics_deck_line',
		label: 'Deck lyric line',
		group: 'performance',
		keywords: ['lyrics', 'deck', 'line', 'karaoke', 'now playing'],
		title: 'Current lyric line in the deck panel',
		detail:
			'Shows the sounding line (and the inbound one when space allows) under the hot cues. Lines the witness distrusts render dimmed rather than confidently.',
		implemented: true,
		control: { kind: 'boolean' }
	},
	HIDE_TODO_SETTING,
	DEV_UI_SETTING,

	// ----- cloudsync (specs/cloudsync-spec.md D5) -------------------------
	// Navigation entries to the policy matrix / pin list / overview on /cloudsync.
	// Full CRUD backing every row there is live at /api/v1/cloudsync/*
	// (apps/webui/server/routes/cloudsync.py).
	{
		id: 'cloudsync.machines',
		label: 'CloudSync: machines & asset policy',
		group: 'cloudsync',
		keywords: [
			'cloudsync', 'cloud', 'sync', 'r2', 'stem', 'stems', 'policy',
			'machine', 'pin', 'pinned', 'cache', 'cached', 'stream', 'excluded'
		],
		title: 'Open the CloudSync policy matrix (/cloudsync)',
		detail:
			'Per-machine, per-asset-kind sync policy (pinned/cached/stream/excluded) for audio, stem bundles, ANLZ cache, vocal cache, lyrics cache, and karaoke word timings. GET+PUT /api/v1/cloudsync/policies.',
		implemented: true,
		control: { kind: 'link', href: '/cloudsync?tab=policies' }
	},
	{
		id: 'cloudsync.playlist_pins',
		label: 'CloudSync: playlist pins',
		group: 'cloudsync',
		keywords: ['cloudsync', 'playlist', 'pin', 'gig', 'crate', 'local', 'set'],
		title: 'Open CloudSync playlist pins (/cloudsync)',
		detail:
			'Pin specific playlists pinned/cached/stream per machine, e.g. keep a gig crate fully local. GET+PUT /api/v1/cloudsync/playlist-pins.',
		implemented: true,
		control: { kind: 'link', href: '/cloudsync?tab=pins' }
	},
	{
		id: 'cloudsync.overview',
		label: 'CloudSync: fleet overview',
		group: 'cloudsync',
		keywords: ['cloudsync', 'overview', 'fleet', 'hydrate', 'hydrated', 'unhydrated', 'last sync'],
		title: 'Open the CloudSync fleet overview (/cloudsync)',
		detail:
			'Per-machine pinned/cached/stream track counts, unhydrated-pinned count, and last sync time. GET /api/v1/cloudsync/overview.',
		implemented: true,
		control: { kind: 'link', href: '/cloudsync?tab=overview' }
	},

	...REKORDBOX_PARITY_STUBS,
	{
		id: 'crossfade_curve',
		label: 'Crossfader curve',
		group: 'performance',
		keywords: ['crossfader', 'curve', 'magic', 'xfade'],
		title: 'Crossfader blend curve',
		detail: 'Magic crossfader is live; other curves are placeholders until built.',
		implemented: true,
		control: {
			kind: 'enum',
			options: [{ value: 'magic', label: 'magic crossfader' }]
		}
	},
	{
		id: 'horizontal_wheel_knob',
		label: 'Horizontal wheel adjusts',
		group: 'performance',
		keywords: ['wheel', 'horizontal', 'filter', 'color', 'knob'],
		title: 'Which knob horizontal mouse wheel turns on selected channels',
		detail: 'Filter is the channel FILTER dial. Color routes to FILTER until color FX is built.',
		implemented: true,
		control: {
			kind: 'enum',
			options: [
				{ value: 'filter', label: 'Filter' },
				{ value: 'color', label: 'Color' }
			]
		}
	},
	...DJAY_PARITY_STUBS
];

/**
 * Both wheel-sensitivity rows share one control shape; only the persisted
 * entry each one writes differs. Bounds and step are read from the module that
 * validates them, so a slider can never offer a factor `setWheelSensitivity`
 * would reject, and widening the validator widens the slider in one edit.
 */
function _wheelSensitivityControl(defaultValue: number): SettingControl {
	return {
		kind: 'number',
		min: WHEEL_SENSITIVITY_MIN,
		max: WHEEL_SENSITIVITY_MAX,
		step: WHEEL_SENSITIVITY_STEP,
		defaultValue,
		unit: 'x'
	};
}

export function groupLabel(id: SettingGroupId): string {
	return SETTING_GROUPS.find((g) => g.id === id)?.label ?? id;
}

export type LinkSettingDef = SettingDef & { control: { kind: 'link'; href: string } };

/** Implemented link-kind rows in catalog order for a settings group. */
export function catalogLinkSettings(group: SettingGroupId): LinkSettingDef[] {
	return SETTINGS_CATALOG.filter(
		(def): def is LinkSettingDef =>
			def.group === group && def.implemented && def.control.kind === 'link'
	);
}
