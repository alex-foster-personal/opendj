import type { SettingDef } from './catalog-types';

/** WAVE-EQ-5230: deck waveforms follow the live EQ and filter (Serato/rekordbox). */
export const WAVE_EQ_REFLECT_SETTING: SettingDef = {
	id: 'wave_eq_reflect',
	label: 'Waveforms follow EQ',
	group: 'performance',
	keywords: [
		'waveform',
		'eq',
		'equalizer',
		'filter',
		'kill',
		'low',
		'mid',
		'high',
		'bass',
		'serato',
		'rekordbox'
	],
	title: "Deck waveforms shrink and grow with each deck's EQ and filter",
	detail:
		"When on (default), cutting a deck's low, mid or high EQ, or sweeping its filter, shrinks that band on the deck's wavestack row and overview strip, as Serato and rekordbox do. The gains come from the Rust audio engine (?engine=rust), which reports the strip it actually runs; the Web Audio engine does not report them yet, so there the waveform stays unscaled. Same path as the wave_eq_reflect performance command.",
	implemented: true,
	control: { kind: 'boolean' }
};
