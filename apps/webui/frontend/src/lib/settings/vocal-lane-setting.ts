import type { SettingDef } from './catalog-types';

// Vocal lane visibility (issue #10238). Split out of catalog.ts, which sits at the
// frontend file-size gate, the same way tempo-lane-setting.ts is.
export const VOCAL_LANE_SETTING: SettingDef = {
	id: 'show_vocal_lane',
	label: 'Show vocal lane',
	group: 'performance',
	keywords: ['vocal', 'vocals', 'voice', 'lane', 'bars', 'blue', 'overlay', 'waveform', 'singing'],
	title: 'Paint where vocals are, as thin blue bars along the top of the deck waveforms',
	detail:
		'When on (default), analyzed vocal regions (rekordbox or own analysis) paint as blue bars on the main scrolling waveform and the deck overview strip. Off hides them; the vocals tooltip stays. Also settable with PUT /api/v1/ui-prefs (show_vocal_lane) and opendj set_waveform_overlay vocal_lane on|off.',
	implemented: true,
	control: { kind: 'boolean' }
};
