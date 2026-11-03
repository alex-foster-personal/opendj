import type { SettingDef } from './catalog-types';

// Tempo-change lane (PR #8342). Split out of catalog.ts, which sits at the
// frontend file-size gate, the same way show-sections-setting.ts is.
export const TEMPO_LANE_SETTINGS: readonly SettingDef[] = [
	{
		id: 'show_tempo_lane',
		label: 'Show tempo changes',
		group: 'performance',
		keywords: ['tempo', 'bpm', 'drift', 'change', 'curve', 'lane', 'beatgrid', 'live', 'overlay'],
		title: 'Paint where the beatgrid tempo moves, on the deck strip and the main waveform',
		detail:
			'When on (default), a track whose beatgrid tempo changes shows a tempo lane on the deck overview strip and a tempo curve over the main scrolling waveform. A track with a steady grid paints nothing. Also settable with PUT /api/v1/ui-prefs (show_tempo_lane).',
		implemented: true,
		control: { kind: 'boolean' }
	},
	{
		id: 'tempo_lane_scale',
		label: 'Tempo curve scale',
		group: 'performance',
		keywords: ['tempo', 'bpm', 'drift', 'curve', 'lane', 'scale', 'relative', 'percent'],
		title: 'How far off nominal BPM the tempo curve’s full height means',
		detail:
			'Fixed 10% (default): the curve reaches full height at 10 percent off the nominal BPM, so tracks compare on one scale. Relative: full height is the track’s own largest tempo change (at least 2 percent), so small drifts are easier to see. Also settable with PUT /api/v1/ui-prefs (tempo_lane_scale).',
		implemented: true,
		control: {
			kind: 'enum',
			options: [
				{ value: 'fixed_10', label: 'Fixed (10% full height)' },
				{ value: 'relative', label: 'Relative (track’s largest change)' }
			]
		}
	}
];
