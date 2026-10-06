import type { SettingDef } from './catalog-types';

/** Issue #4219: waveform band palette; Auto (follow the skin) is the default. */
export const WAVE_PALETTE_SETTING: SettingDef = {
	id: 'wave_palette',
	label: 'Waveform colors',
	group: 'performance',
	keywords: ['waveform', 'color', 'colors', 'palette', 'auto', 'skin', 'band', '3band', 'rekordbox', 'cdj', 'legacy', 'blue', 'orange', 'amber'],
	title: 'Waveform frequency-band colors',
	detail:
		'Auto (default) follows the skin: Gothic uses Mono grayscale, Default and Light use rekordbox 3Band. rekordbox 3Band matches the CDJ: dark blue lows, amber mids, white highs. Legacy is the earlier Open DJ palette: orange lows, blue mids, near-white highs. Applies to the deck waveforms and the overview and preview strips, in both light and dark themes.',
	implemented: true,
	preview: 'waveform',
	control: {
		kind: 'enum',
		options: [
			{ value: 'auto', label: 'Auto (follows skin)' },
			{ value: 'rekordbox', label: 'rekordbox 3Band' },
			{ value: 'legacy', label: 'Legacy (orange lows)' },
			{ value: 'mono', label: 'Mono grayscale' }
		]
	}
};

/** Preview skin: near-black grayscale chrome (html[data-skin='mono-dev']). */
export const UI_SKIN_SETTING: SettingDef = {
	id: 'ui_skin',
	label: 'UI skin',
	group: 'performance',
	keywords: ['skin', 'theme', 'light', 'gothic', 'mono', 'grayscale', 'gray', 'minimal', 'dev'],
	title: 'Chrome skin layered over the light/dark theme',
	detail:
		'Gothic (default) is a near-black grayscale skin with hairline borders, square controls and monospace type. Default keeps the rekordbox-style chrome. Light is the warm light skin. The top-bar skin button cycles Default, Gothic, Light. With Waveform design and Waveform colors on Auto, the waveforms follow the skin.',
	implemented: true,
	control: {
		kind: 'enum',
		options: [
			{ value: 'default', label: 'Default' },
			{ value: 'mono-dev', label: 'Gothic (mono dev preview)' },
			{ value: 'light', label: 'Light' }
		]
	}
};

/** Split main waveform: master deck on top, this deck below. Opt-in. */
export const WAVE_SPLIT_MASTER_SETTING: SettingDef = {
	id: 'wave_split_master',
	label: 'Split waveform (master on top)',
	group: 'performance',
	keywords: ['split', 'master', 'waveform', 'beat', 'matching', 'phase', 'wavestack'],
	title: 'Deck waveform rows: master on the top half, this deck on the bottom half',
	detail:
		'Opt-in, intended for a future decks-only view. Off (and Auto, on every skin) paints the standard mirrored waveform. On: each deck row paints the MASTER deck above the centerline and this deck below it, each on its own playhead and tempo, so in-phase beats meet at the line; the master deck keeps its own mirrored waveform. Same path as the set_skin performance command.',
	implemented: true,
	control: {
		kind: 'enum',
		options: [
			{ value: 'auto', label: 'Auto (off)' },
			{ value: 'on', label: 'On' },
			{ value: 'off', label: 'Off' }
		]
	}
};
