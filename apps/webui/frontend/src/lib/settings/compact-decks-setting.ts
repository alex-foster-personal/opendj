import type { SettingDef } from './catalog-types';

// LIBUX-11716. Split out of catalog.ts, which sits at the frontend file-size gate.
export const COMPACT_DECKS_SETTING: SettingDef = {
	id: 'compact_decks',
	label: 'Compact decks',
	group: 'performance',
	keywords: ['compact', 'decks', '4-deck', 'library', 'rows', 'laptop', 'small screen', 'height'],
	title: 'Shrink the decks in 4-deck view so the library shows more rows',
	detail:
		'Off by default. When on, only in 4-deck (MORE) view: smaller waveform rows, overview strip, deck artwork, deck title font and mixer EQ knobs and fader floor, so more library rows fit on a 13-inch laptop.',
	implemented: true,
	control: { kind: 'boolean' }
};
