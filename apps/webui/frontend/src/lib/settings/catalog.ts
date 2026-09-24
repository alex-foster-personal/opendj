/**
 * Warp-style settings catalog: working prefs + grayed PARITY-TODO stubs.
 * Search filters this list in-place (results ARE the settings).
 */

import { APP_POSTURE_SETTING } from './app-posture-setting';
import { PREVIEW_BEAT_SYNC_SETTING } from './preview-beat-sync-setting';
import {
	WHEEL_SENSITIVITY,
	WHEEL_SENSITIVITY_MAX,
	WHEEL_SENSITIVITY_MIN,
	WHEEL_SENSITIVITY_STEP
} from '$lib/rb/wheel-adjust';

export type { SettingDef, SettingGroupId } from './catalog-types';
import type { SettingControl, SettingDef, SettingGroup, SettingGroupId } from './catalog-types';

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

const TODO = 'not implemented - see PARITY-TODO';

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
	{
		id: 'hide_todo_settings',
		label: 'Hide todo / grayed settings',
		group: 'advanced',
		keywords: ['todo', 'gray', 'parity', 'hide', 'stub', 'placeholder'],
		title: 'Hide PARITY-TODO placeholder settings from the list',
		detail: 'When on, only implemented settings appear in search results and category lists.',
		implemented: true,
		control: { kind: 'boolean' }
	},

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

	// ----- rekordbox parity stubs ----------------------------------------
	_todo('rb.quantize', 'Quantize', 'rekordbox', ['quantize', 'grid'], 'Global quantize default'),
	_todo('rb.sync_mode', 'Beat Sync mode default', 'rekordbox', ['sync', 'bar', 'beat'], 'BAR vs BEAT sync default'),
	_todo('rb.master_tempo', 'Master Tempo default', 'rekordbox', ['key', 'lock', 'tempo'], 'Keep original key when pitching'),
	_todo('rb.vinyl_mode', 'Vinyl mode', 'rekordbox', ['vinyl', 'scratch', 'cdj'], 'Jog vinyl / CDJ feel'),
	_todo('rb.jog_sensitivity', 'Jog sensitivity', 'rekordbox', ['jog', 'platter'], 'Platter touch sensitivity'),
	_todo('rb.hot_cue_colors', 'Hot cue color map', 'rekordbox', ['cue', 'color', 'pad'], 'Pad color scheme'),
	_todo('rb.pad_mode', 'Pad mode memory', 'rekordbox', ['pad', 'hotcue', 'sampler'], 'Remember last pad bank'),
	_todo('rb.waveform_zoom', 'Waveform zoom default', 'rekordbox', ['waveform', 'zoom'], 'Default overview zoom'),
	_todo('rb.grid_edit', 'Allow beat grid edit', 'rekordbox', ['grid', 'beat', 'edit'], 'Enable grid nudge/edit'),
	_todo('rb.auto_gain', 'Auto gain', 'rekordbox', ['gain', 'loudness'], 'Normalize channel gain on load'),
	_todo('rb.karaoke', 'Karaoke / vocal mute', 'rekordbox', ['karaoke', 'vocal'], 'Vocal-oriented mute presets'),
	_todo('rb.export_usb', 'USB export defaults', 'rekordbox', ['usb', 'export', 'device'], 'Device export preferences'),
	_todo('rb.analysis_quality', 'Analysis quality', 'rekordbox', ['analysis', 'anlz', 'pqtz'], 'BPM/key analysis quality'),
	_todo('rb.phrase_analysis', 'Phrase analysis', 'rekordbox', ['phrase', 'structure'], 'Enable phrase detection'),
	_todo('rb.mytag_layout', 'MyTag layout', 'rekordbox', ['mytag', 'tag'], 'MyTag browser layout'),
	_todo('rb.track_info_fields', 'Track info fields', 'rekordbox', ['info', 'columns'], 'Which columns show in info'),
	_todo('rb.keyboard_map', 'Keyboard mapping', 'rekordbox', ['keyboard', 'shortcuts', 'midi'], 'Custom key bindings'),
	_todo('rb.dual_deck_layout', 'Dual deck layout', 'rekordbox', ['layout', '2deck', '4deck'], '2 vs 4 deck chrome'),

	// ----- djay Pro stubs ------------------------------------------------
	_todo('djay.automix', 'Automix', 'djay', ['automix', 'auto'], 'Automix transitions'),
	_todo('djay.eq_kill', 'EQ kill switches', 'djay', ['eq', 'kill'], 'Instant EQ kills'),
	_todo('djay.effects_rack', 'Effects rack layout', 'djay', ['fx', 'effects'], 'FX slot layout'),
	_todo('djay.stems_ui', 'Stems mixer UI', 'djay', ['stems', 'vocal', 'drums'], 'Stem fader visibility'),
	_todo('djay.library_source', 'Library source', 'djay', ['itunes', 'apple', 'spotify'], 'External library source'),
	_todo('djay.streaming', 'Streaming services', 'djay', ['tidal', 'soundcloud', 'beatport'], 'Connected streaming'),
	_todo('djay.cue_points', 'Cue point style', 'djay', ['cue', 'points'], 'Cue marker style'),
	_todo('djay.crossfader_curve', 'Crossfader curve', 'djay', ['crossfader', 'curve'], 'XF curve shape'),
	_todo('djay.midi_learn', 'MIDI learn', 'djay', ['midi', 'map', 'controller'], 'Controller MIDI learn'),
	_todo('djay.audio_device', 'Audio device', 'djay', ['device', 'output', 'asio'], 'Output device selection'),
	_todo('djay.sample_rate', 'Sample rate', 'djay', ['sample', 'rate', '48000'], 'Engine sample rate'),
	_todo('djay.buffer_size', 'Buffer size', 'djay', ['buffer', 'latency'], 'Audio buffer / latency'),
	_todo('djay.recording', 'Session recording', 'djay', ['record', 'rec', 'session'], 'REC defaults'),
	_todo('djay.video', 'Video deck', 'djay', ['video', 'visual'], 'Video deck enable'),
	_todo('djay.neumann', 'NEUMANN UI scale', 'djay', ['ui', 'scale', 'retina'], 'Interface scaling')
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

function _todo(
	id: string,
	label: string,
	group: SettingGroupId,
	keywords: string[],
	title: string
): SettingDef {
	return {
		id,
		label,
		group,
		keywords,
		title: TODO,
		detail: `${title}. ${TODO}`,
		implemented: false,
		control: { kind: 'boolean' }
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
