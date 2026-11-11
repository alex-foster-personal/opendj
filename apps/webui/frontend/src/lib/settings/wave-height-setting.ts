import type { SettingDef } from './catalog-types';

/** WAVEQS-11733: deck waveform drawn mirrored or single-sided. */
export const WAVE_HEIGHT_SETTING: SettingDef = {
	id: 'wave_height',
	label: 'Waveform height',
	group: 'performance',
	keywords: [
		'waveform',
		'height',
		'half',
		'full',
		'mirrored',
		'single-sided',
		'one-sided',
		'cdj',
		'serato',
		'rekordbox'
	],
	title: 'Deck waveform drawn mirrored or single-sided',
	detail:
		"Full (default) mirrors each deck's waveform about the centre line. Half draws only the upper envelope, rising from the row's bottom edge, as CDJ players and Serato do; some DJs read transients better that way. Cue markers, the beat grid and lyrics are unchanged. The split master waveform keeps its own layout. Same path as opendj set_waveform_design height=half.",
	implemented: true,
	preview: 'waveform',
	control: {
		kind: 'enum',
		options: [
			{ value: 'full', label: 'Full (mirrored)' },
			{ value: 'half', label: 'Half (upper half only)' }
		]
	}
};
