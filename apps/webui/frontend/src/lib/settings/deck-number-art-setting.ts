import type { SettingDef } from './catalog-types';

// ARTNUM-9649. Split out of catalog.ts, which sits at the frontend file-size gate.
export const DECK_NUMBER_ART_SETTING: SettingDef = {
	id: 'deck_number_art',
	label: 'Deck number on artwork',
	group: 'appearance',
	keywords: ['deck', 'number', 'artwork', 'cover', 'outline', 'overlay', 'identify', 'mixer'],
	title: 'Draw each deck number as an outline over its track artwork',
	detail:
		'Off by default. Outline + halo draws the deck number (1 to 4) over the artwork in the deck header, the waveform gutter and the mixer column, so the art identifies the deck. Skipped on the short-window gutter, where the art is under 40px.',
	implemented: true,
	control: {
		kind: 'enum',
		options: [
			{ value: 'off', label: 'Off' },
			{ value: 'halo', label: 'Outline + halo' }
		]
	}
};
