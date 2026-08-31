/**
 * Warp-style settings catalog: working prefs + grayed PARITY-TODO stubs.
 * Search filters this list in-place (results ARE the settings).
 */

export type SettingGroupId =
	| 'appearance'
	| 'library'
	| 'performance'
	| 'confirmations'
	| 'sync'
	| 'cloudsync'
	| 'advanced'
	| 'rekordbox'
	| 'djay';

export interface SettingGroup {
	id: SettingGroupId;
	label: string;
}

export type SettingControl =
	| { kind: 'boolean' }
	| { kind: 'enum'; options: ReadonlyArray<{ value: string; label: string }> }
	| {
			kind: 'multi_bool';
			keys: ReadonlyArray<{ id: string; label: string; title: string }>;
	  }
	// Pure-navigation entry: no inline widget, just a searchable pointer to a
	// full route page. SettingsOverlay.svelte's control-kind branches
	// (boolean/enum/multi_bool) do not render one of these -- by design,
	// this lane could not touch that shared component (fan-out file
	// ownership) -- so today the row is discoverable and shows its target in
	// `detail`/`title`, but clicking it does not yet navigate. A follow-up
	// in SettingsOverlay.svelte adding `{:else if kind === 'link'}<a href=...>`
	// (and a matching branch in `activateSetting`) makes it clickable.
	| { kind: 'link'; href: string };

export interface SettingDef {
	id: string;
	label: string;
	group: SettingGroupId;
	keywords: readonly string[];
	/** Short RHS hover tooltip. */
	title: string;
	/** Longer explanation shown on focus/hover. */
	detail: string;
	/** false = grayed inert todo (PARITY-TODO). */
	implemented: boolean;
	control: SettingControl;
}

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
		label: 'Next-only library filter',
		group: 'library',
		keywords: ['next', 'camelot', 'bpm', 'tab', 'suggest', 'compatible'],
		title: 'Keep only Camelot+BPM-compatible next tracks',
		detail: 'Filters the library list vs the loaded/master reference. Also toggled with Tab on /performance.',
		implemented: true,
		control: { kind: 'boolean' }
	},
	{
		id: 'beat_sync_max',
		label: 'Beat Sync Max',
		group: 'performance',
		keywords: ['beatsync', 'bar', 'phase', 'seek', 'sync', 'master'],
		title: 'Phase-preserving BAR sync on every relocate',
		detail:
			'When on, every transport relocate (including master) keeps BAR phase lock across synced decks.',
		implemented: true,
		control: { kind: 'boolean' }
	},
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
			'Saves which vendors you want auto-sync to target. Vendor DB writeback is not automatic yet - use apps/sync/apply_ratings.py (CLI). Preference + plumbing only.',
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
		title: TODO,
		detail:
			'Will call apply_ratings / OpenDJ writeback when wired. Today: preference destinations above + manual CLI only.',
		implemented: false,
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
	// Navigation entries only (no inline widget) -- the real controls are
	// the policy matrix / pin list / overview table on the /cloudsync route.
	// Full CRUD backing every row there is live at /api/v1/cloudsync/*
	// (apps/webui/server/routes/cloudsync.py); implemented: true is correct
	// even though these specific catalog rows are not yet click-to-navigate
	// (see the 'link' control-kind comment above).
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
			'Per-machine, per-asset-kind sync policy (pinned/cached/stream/excluded) for audio, stem bundles, ANLZ cache, and vocal cache. GET+PUT /api/v1/cloudsync/policies.',
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

export function settingById(id: string): SettingDef | undefined {
	return SETTINGS_CATALOG.find((s) => s.id === id);
}

export function groupLabel(id: SettingGroupId): string {
	return SETTING_GROUPS.find((g) => g.id === id)?.label ?? id;
}
