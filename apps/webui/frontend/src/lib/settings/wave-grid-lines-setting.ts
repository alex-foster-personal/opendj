import type { SettingDef } from './catalog-types';

// WAVEQS-11736 (issue #10244). Serato DJ Pro's display options can hide the
// beat grid and rekordbox draws quieter grid lines; clean room, behaviour only.
export const WAVE_GRID_LINES_SETTING: SettingDef = {
	id: 'wave_grid_lines',
	label: 'Beat grid lines',
	group: 'performance',
	keywords: [
		'beat',
		'grid',
		'beatgrid',
		'bar',
		'bars',
		'downbeat',
		'lines',
		'waveform',
		'hide',
		'serato',
		'rekordbox'
	],
	title: 'Beat grid lines on the deck waveforms',
	detail:
		'Beats and bars (default) draws every beat line with stronger downbeat lines. Bars only keeps the downbeat lines. Off hides the grid. Beat sync, quantize and snapping still use the grid. Also settable with PUT /api/v1/ui-prefs (wave_grid_lines) and opendj set_waveform_overlay grid_lines value=bars.',
	implemented: true,
	preview: 'waveform',
	control: {
		kind: 'enum',
		options: [
			{ value: 'beats_bars', label: 'Beats and bars' },
			{ value: 'bars', label: 'Bars only' },
			{ value: 'off', label: 'Off' }
		]
	}
};
