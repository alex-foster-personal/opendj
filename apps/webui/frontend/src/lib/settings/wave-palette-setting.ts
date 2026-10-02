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
			{ value: 'legacy', label: 'Legacy (orange lows)' }
		]
	}
};
