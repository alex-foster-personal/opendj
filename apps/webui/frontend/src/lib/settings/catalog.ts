/**
 * Warp-style settings catalog: working prefs only (SETSTUB-01).
 * Search filters this list in-place (results ARE the settings).
 */
import { APP_POSTURE_SETTING } from './app-posture-setting';
import { GIG_HELPER_SETTING } from './gig-helper-setting';
import { AUDIO_ENGINE_SETTING } from './audio-engine-setting';
import { PREVIEW_BEAT_SYNC_SETTING } from './preview-beat-sync-setting';
import {
	DRAG_BASE_MAX,
	DRAG_BASE_MIN,
	DRAG_BASE_STEP,
	DRAG_DEFAULTS,
	DRAG_FALLOFF_MAX_PX,
	DRAG_FALLOFF_MIN_PX,
	DRAG_FALLOFF_STEP_PX
} from '$lib/rb/drag-sensitivity.svelte';
import { AUTO_GAIN_ENABLED_SETTING, AUTO_GAIN_TARGET_SETTING } from './auto-gain-setting';
import { MIDI_ENABLED_SETTING } from './midi-enabled-setting';
import { CONFIRMATION_SETTINGS } from './confirmation-settings';
import {
	LIBRARY_LAYOUT_SETTING,
	PERFORMANCE_LAYOUT_SETTING,
	UI_SKIN_SETTING,
	WAVE_PALETTE_SETTING,
	WAVE_SPLIT_MASTER_SETTING
} from './wave-palette-setting';
import { WAVE_EQ_REFLECT_SETTING } from './wave-eq-reflect-setting';
import { WAVE_ZOOM_SETTINGS } from './wave-zoom-setting';
import { DEV_UI_SETTING, HIDE_TODO_SETTING } from './dev-ui-setting';
import { VARIANT_REGISTRY } from './variant-setting';
import { SHOW_SECTIONS_SETTING } from './show-sections-setting';
import { DECK_NUMBER_ART_SETTING } from './deck-number-art-setting';
import { SPEAKER_AUTO_RECOVER_SETTING } from './speaker-link-setting';
import { TEMPO_LANE_SETTINGS } from './tempo-lane-setting';
import {
	PIN_VISIBLE_DAYS_DEFAULT,
	PIN_VISIBLE_DAYS_MAX,
	PIN_VISIBLE_DAYS_MIN
} from '$lib/rb/feedback-pin-age';
import {
	WHEEL_SENSITIVITY,
	WHEEL_SENSITIVITY_MAX,
	WHEEL_SENSITIVITY_MIN,
	WHEEL_SENSITIVITY_STEP
} from '$lib/rb/wheel-adjust';

export type { LinkSettingDef, SettingDef, SettingGroupId } from './catalog-types';
import type { LinkSettingDef, SettingControl, SettingDef, SettingGroup, SettingGroupId } from './catalog-types';

export const SETTING_GROUPS: readonly SettingGroup[] = [
	{ id: 'appearance', label: 'Appearance' },
	{ id: 'library', label: 'Library' },
	{ id: 'performance', label: 'Performance' },
	{ id: 'confirmations', label: 'Confirmations' },
	{ id: 'sync', label: 'Sync' },
	{ id: 'cloudsync', label: 'CloudSync' },
	{ id: 'advanced', label: 'Advanced' }
];

/** Every row is a working setting (SETSTUB-01: the catalog ships no
 * implemented:false placeholder rows). */
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
		id: 'pin_visible_days',
		label: 'How many days should pins be visible?',
		group: 'appearance',
		keywords: ['pin', 'pins', 'comment', 'comments', 'feedback', 'days', 'visible', 'hide', 'age'],
		title: 'Hide comment pins older than this many days',
		detail:
			'0 shows every pin. Any other number hides comment pins older than that many days. The same number is on the comment-pin hover. Saved in this browser only.',
		implemented: true,
		control: {
			kind: 'number',
			min: PIN_VISIBLE_DAYS_MIN,
			max: PIN_VISIBLE_DAYS_MAX,
			step: 1,
			defaultValue: PIN_VISIBLE_DAYS_DEFAULT,
			unit: ' days'
		}
	},
	{
		id: 'library_sidebar_art',
		label: 'Playlist art in the sidebar',
		group: 'library',
		keywords: ['thumbnail', 'cover', 'artwork', 'mosaic', 'spotify', 'tiles', 'grid', 'image'],
		title: 'Show a cover tile beside each playlist in the sidebar',
		detail:
			'Each playlist row shows a 48 px tile: a 2x2 mosaic of its first four different covers, its one cover, or a note when it has no art. Works in every skin. Default on.',
		implemented: true,
		control: { kind: 'boolean' }
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
		label: 'Watched folders',
		group: 'library',
		keywords: ['watcher', 'watch', 'folder', 'import', 'auto', 'monitor', 'ingest'],
		title: 'Folders Open DJ checks for new audio while it runs',
		detail: 'One absolute path per line, checked to exist on save. Rescanned about once a minute.',
		implemented: true,
		control: {
			kind: 'path_lines',
			notice:
				'Checked about every 60 s while Open DJ runs. New audio files join the library; a file that disappears is marked missing and comes back on its own. Open DJ only reads these folders and never changes your files.'
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
		id: 'key_notation',
		label: 'Key notation',
		group: 'library',
		keywords: ['key', 'camelot', 'open key', 'musical', 'notation', 'traktor', 'mik'],
		title: 'How every key is written',
		detail:
			'One notation for every key: Camelot and musical together (8A \u2022 Am, the default), Camelot (8A), Open Key (1m) or musical (Am). Applies to the library table, deck headers and the inspector. Keys stored in any notation are converted, so nothing mixes them.',
		implemented: true,
		control: {
			kind: 'enum',
			options: [
				{ value: 'camelot_musical', label: 'Camelot + musical (8A \u2022 Am)' },
				{ value: 'camelot', label: 'Camelot (8A)' },
				{ value: 'open_key', label: 'Open Key (1m)' },
				{ value: 'musical', label: 'Musical (Am)' }
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
			"Shown as the 'compatible' checkbox in the library header. Filters the library list vs the master, else playing, else any loaded with key and BPM (the mode's key rule with any allowed key shift, the BPM window, half/double folds). Also toggled with Tab on /performance.",
		implemented: true,
		control: { kind: 'boolean' }
	},
	{
		id: 'compatible_filter.mode',
		label: 'Compatible filter mode',
		group: 'library',
		keywords: ['compatible', 'mashup', 'transition', 'key', 'camelot', 'acapella', 'harmonic'],
		title: 'Which key rule the compatible filter uses: Transition or Mashup',
		detail:
			'Transition: the Camelot rules ticked in the compatible dropdown (by default the same key, ±1 on the same letter and the relative major/minor), good for a 16 to 32 bar blend. Mashup: the exact same key, for an acapella or a lead layered over a whole track, where small clashes add up. Both allow the key shift set below. Also switched from the compatible dropdown in the library header.',
		implemented: true,
		control: {
			kind: 'enum',
			options: [
				{ value: 'transition', label: 'Transition' },
				{ value: 'mashup', label: 'Mashup' }
			]
		}
	},
	{
		id: 'compatible_filter.mashup_relative',
		label: 'Mashup: also allow relative major/minor',
		group: 'library',
		keywords: ['compatible', 'mashup', 'relative', 'major', 'minor', 'key', 'acapella'],
		title: 'Mashup mode also matches the relative major/minor (8A with 8B)',
		detail: 'Off (default): Mashup needs the exact same key, because a mashup layers two parts for minutes rather than bars. On: the relative major/minor counts too.',
		implemented: true,
		control: { kind: 'boolean' }
	},
	{
		id: 'compatible_filter.key_shift_max',
		label: 'Allowed key shift',
		group: 'library',
		keywords: ['compatible', 'key', 'shift', 'semitone', 'pitch', 'transpose', 'mashup', 'master tempo', 'key lock'],
		title: 'How many semitones a track may be shifted to count as compatible',
		detail:
			'A track that matches only after a shift shows the shift as a superscript on its key (8A⁻¹) and loads shifted by it. 1 semitone is about a 6% stretch and sounds clean with key lock (Mashup: 3 of 24 keys match, Transition with the default rules: 11 of 24). 2 is about 12%: vocals start to color (Mashup: 5 of 24, Transition with the default rules: 17 of 24). 3 is about 19%: vocals sound chipmunky or muddy and transients smear (Mashup: 7 of 24). Why a semitone: (private path).',
		implemented: true,
		control: {
			kind: 'enum',
			options: [
				{ value: '0', label: 'Off' },
				{ value: '1', label: '1 semitone' },
				{ value: '2', label: '2 semitones' },
				{ value: '3', label: '3 semitones' }
			]
		}
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
		title: 'Turn Beat Sync on for every loaded deck, BAR downbeat lock held',
		detail:
			'Turning it on turns Beat Sync on for every deck with a track loaded in an open performance view (otherwise each deck syncs on its next load), and for each deck as a track loads; synced playing decks then keep PQTZ n=1 aligned on every relocate and over playback. A deck SYNC press still turns that deck off until its next load. Turning it off changes no deck; followers then sync on seek using their own BEAT/BAR mode and the master free-seeks.',
		implemented: true,
		control: { kind: 'boolean' }
	},
	{
		id: 'deck_default_quantize',
		label: 'Quantize on new decks',
		group: 'performance',
		keywords: ['quantize', 'grid', 'snap', 'default', 'deck', 'q'],
		title: 'Quantize state each deck starts with',
		detail:
			'The Q button state a deck starts with when the page opens or the deck is ejected; changing it updates every deck with no track loaded. A loaded deck keeps its own Q. Default on.',
		implemented: true,
		control: { kind: 'boolean' }
	},
	{
		id: 'deck_needle_lock',
		label: 'Needle Lock',
		group: 'performance',
		keywords: ['needle', 'lock', 'overview', 'strip', 'seek', 'search', 'touch'],
		title: 'A playing deck ignores clicks on its overview strip',
		detail:
			'On: a click on a playing deck\'s overview strip does not seek, and a short hint says why; a paused deck still seeks. The main scrolling waveform and keyboard jumps are not affected. Default off.',
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
		title: 'Fade regions in/out on edge-reveal (Ctrl+R overlay mode)',
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
	MIDI_ENABLED_SETTING,
	AUTO_GAIN_ENABLED_SETTING,
	AUTO_GAIN_TARGET_SETTING,
	SHOW_SECTIONS_SETTING,
	DECK_NUMBER_ART_SETTING,
	SPEAKER_AUTO_RECOVER_SETTING,
	...TEMPO_LANE_SETTINGS,
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
		id: 'deck_hover_link',
		label: 'Deck hover link highlight',
		group: 'performance',
		keywords: ['hover', 'mixer', 'tick'],
		title: 'Hovering a deck marks its waveform, mixer and library rows',
		detail: "Off hides the accent tick on the hovered deck's panels.",
		implemented: true,
		control: { kind: 'boolean' }
	},
	{
		id: 'waveform_design',
		label: 'Waveform design',
		group: 'performance',
		keywords: ['waveform', 'design', 'auto', 'skin', 'tri-band', 'mono', 'line', 'blocks', 'wavestack', 'strip'],
		title: 'Deck and library waveform paint style',
		detail:
			'Auto (default) follows the skin: Gothic paints blocks, Default and Light paint tri-band. Tri-band matches rekordbox-style stacked frequency bands. Mono draws a single envelope. Line draws a stroke outline. Blocks draws bars stacked in the band colors (shades of gray in the Mono palette). The preview shows the effective design for the current skin, painted by the deck renderer itself on a real 12 s track sample.',
		implemented: true,
		preview: 'waveform',
		control: {
			kind: 'enum',
			options: [
				{ value: 'auto', label: 'Auto (follows skin)' },
				{ value: 'tri-band', label: 'Tri-band bars' },
				{ value: 'mono', label: 'Mono envelope' },
				{ value: 'line', label: 'Line outline' },
				{ value: 'blocks', label: 'Blocks (mono bars)' }
			]
		}
	},
	WAVE_PALETTE_SETTING,
	WAVE_EQ_REFLECT_SETTING,
	UI_SKIN_SETTING,
	PERFORMANCE_LAYOUT_SETTING,
	LIBRARY_LAYOUT_SETTING,
	WAVE_SPLIT_MASTER_SETTING,
	...WAVE_ZOOM_SETTINGS,
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
		id: 'mixer_stems',
		label: 'Mixer stem dials (S)',
		group: 'performance',
		keywords: ['stem', 'stems', 'mixer', 'vocal', 'inst', 'bass', 'drums', 'dial', 'knob', 's button'],
		title: 'Stem level dials beside the EQ on every channel with stems',
		detail:
			'The S button left of MORE/LESS. On: each channel whose track has stems shows VOCAL, INST and BASS dials beside HI, MID and LOW, and DRUMS beside FILTER, and the mixer widens 20%. Channels without stems are unchanged. Same as the mixer_stems performance command.',
		implemented: true,
		control: { kind: 'boolean' }
	},
	{
		id: 'feature_icon_single_click',
		label: 'Feature icons: single click',
		group: 'performance',
		keywords: ['icon', 'cluster', 'top bar', 'toggle', 'feature', 'component', 'single click', 'row'],
		title: 'Click each top-bar feature icon on its own',
		detail:
			'The performance top-bar icon strip toggles components on and off. By default the strip is one button: click it and pick components from the list. Core icons stay on the first row of the top bar. More components wrap onto a second row, then a third. Turn this on when each icon should toggle on a single click instead. Saved in this browser only.',
		implemented: true,
		control: { kind: 'boolean' }
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
		label: 'Mirror deck 2 and 4 controls',
		group: 'performance',
		keywords: ['deck', 'mirror', 'symmetry', 'deck 2', 'layout', 'right column'],
		title: 'Mirror deck 2 and 4 main control rows for mixer-facing symmetry',
		detail:
			'When on, deck 2 and 4 control row order is reversed horizontally (block order inside each cluster stays the same). Default off.',
		implemented: true,
		control: { kind: 'boolean' }
	},
	{
		id: 'deck_left_mirror',
		label: 'Mirror deck 1 and 3 controls',
		group: 'performance',
		keywords: ['deck', 'mirror', 'symmetry', 'deck 1', 'deck 3', 'layout', 'left column'],
		title: 'Mirror deck 1 and 3 main control rows',
		detail:
			'When on, deck 1 and 3 control row order is reversed horizontally (block order inside each cluster stays the same). Default off.',
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
			'A macOS trackpad sends a dense burst of wheel events for a single finger movement, so it is scaled separately from a mouse: 1x here means "as sensitive as the mouse". The shipped 1/3 value is an initial starting estimate, derived from WHEEL_TRACKPAD_EVENTS_PER_DETENT in lib/rb/wheel-adjust.ts; tune it for your hardware. Same controls and same persistence as the mouse factor.',
		implemented: true,
		control: _wheelSensitivityControl(WHEEL_SENSITIVITY.trackpad)
	},
	{
		id: 'drag_sensitivity.base',
		label: 'Drag sensitivity',
		group: 'performance',
		keywords: [
			'drag', 'click drag', 'sensitivity', 'mouse', 'knob', 'dial', 'fader', 'eq', 'group', 'gain'
		],
		title: 'How far a click-drag moves a mixer knob or channel fader, as a multiple of the original scale',
		detail:
			'Applies to every knob and channel fader drag, single or grouped; the crossfader keeps its direct pointer mapping. 0.70x is the default ("30% lower as base"). Changes apply at once, no reload. Try it on the test knobs and faders below: they use the same drag code, shift-select groups and the Opt flip, and are wired to nothing.',
		implemented: true,
		tester: 'drag',
		control: {
			kind: 'number',
			min: DRAG_BASE_MIN,
			max: DRAG_BASE_MAX,
			step: DRAG_BASE_STEP,
			defaultValue: DRAG_DEFAULTS.base,
			unit: 'x'
		}
	},
	{
		id: 'drag_sensitivity.falloff_px',
		label: 'Drag falloff distance',
		group: 'performance',
		keywords: ['drag', 'falloff', 'distance', 'fine', 'sensitivity', 'knob', 'fader'],
		title: 'Pointer distance from the press at which a drag moves half as fast',
		detail:
			'The further the pointer gets from where you pressed, the gentler the drag: gain = base / (1 + distance / this). Moving back toward the press gives fine control back, with no jump. Default 150 px.',
		implemented: true,
		control: {
			kind: 'number',
			min: DRAG_FALLOFF_MIN_PX,
			max: DRAG_FALLOFF_MAX_PX,
			step: DRAG_FALLOFF_STEP_PX,
			defaultValue: DRAG_DEFAULTS.falloffPx,
			unit: ' px'
		}
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
		id: 'library.title_fix',
		label: 'Fix broken track metadata',
		group: 'library',
		keywords: [
			'title', 'artist', 'metadata', 'broken', 'fix', 'cleanup', 'junk', 'remix', 'tags',
			'openrouter', 'api key', 'auto-apply', 'spend', 'cap'
		],
		title: 'Open the Title fixes admin pane (/admin?tab=title-fix)',
		detail:
			'Scan for tracks whose artist, key or version sits in the title, then review raw -> proposed diffs and apply or undo them. Config there: enabled, OpenRouter key (masked), auto-apply policy (off / safe tier / gate-approved), spend cap per scan, tag families. GET+PUT /api/v1/title-fix/config; CLI python -m apps.webui.title_fix_cli.',
		implemented: true,
		control: { kind: 'link', href: '/admin?tab=title-fix' }
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
		label: 'Word-aligned waveform lyrics',
		group: 'performance',
		keywords: ['lyrics', 'waveform', 'overlay', 'deck', 'karaoke', 'lanes', 'alignment', 'words', 'zoom'],
		title: 'Words on the main waveforms, each at its sung time',
		detail:
			'Places each word on the deck waveform at its sung onset. Words spread over two rows, and a third row only when two rows would overlap. The default waveform zoom keeps that spacing on the row width you have. This switch is the global default.',
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
	// Taste variants (epic #10045): family modules register via defineVariantSetting
	// and are imported above so ESM evaluates them before this array is built.
	...VARIANT_REGISTRY,

	// ----- cloudsync ((private path) D5) -------------------------
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
	}
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

/** Implemented link-kind rows in catalog order for a settings group. */
export function catalogLinkSettings(group: SettingGroupId): LinkSettingDef[] {
	return SETTINGS_CATALOG.filter(
		(def): def is LinkSettingDef =>
			def.group === group && def.implemented && def.control.kind === 'link'
	);
}
