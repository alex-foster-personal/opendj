import type { SettingDef } from './catalog-types';

/** Issue #4219: waveform band palette (rekordbox 3Band default, legacy kept). */
export const WAVE_PALETTE_SETTING: SettingDef = {
	id: 'wave_palette',
	label: 'Waveform colors',
	group: 'performance',
	keywords: ['waveform', 'color', 'colors', 'palette', 'band', '3band', 'rekordbox', 'cdj', 'legacy', 'blue', 'orange', 'amber'],
	title: 'Waveform frequency-band colors',
	detail:
		'rekordbox 3Band (default) matches the CDJ: dark blue lows, amber mids, white highs. Legacy is the earlier Open DJ palette: orange lows, blue mids, near-white highs. Applies to the deck waveforms and the overview and preview strips, in both light and dark themes.',
	implemented: true,
	control: {
		kind: 'enum',
		options: [
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
	keywords: ['skin', 'theme', 'mono', 'grayscale', 'gray', 'minimal', 'dev'],
	title: 'Chrome skin layered over the light/dark theme',
	detail:
		'Default keeps the rekordbox-style chrome. Mono dev is a near-black grayscale skin with hairline borders, square controls and monospace type. Pair it with Waveform colors: Mono grayscale and Waveform design: Blocks.',
	implemented: true,
	control: {
		kind: 'enum',
		options: [
			{ value: 'default', label: 'Default' },
			{ value: 'mono-dev', label: 'Mono dev (preview)' }
		]
	}
};
