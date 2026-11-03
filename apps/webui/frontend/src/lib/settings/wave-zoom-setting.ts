import type { SettingDef } from './catalog-types';

/** WAVEZOOM-6802..06: the main waveform's zoom and how it behaves. */
export const WAVE_ZOOM_SETTINGS: readonly SettingDef[] = [
	{
		id: 'wave_zoom',
		label: 'Waveform zoom',
		group: 'performance',
		keywords: ['waveform', 'zoom', 'bars', 'whole'],
		title: 'How many bars the deck waveforms show across the row',
		detail:
			'Ctrl/Cmd + wheel, pinch, or [ and ] step it. Bars use the master tempo. IPC: set_wave_zoom.',
		implemented: true,
		control: {
			kind: 'enum',
			options: [
				{ value: '4', label: '4 bars' },
				{ value: '8', label: '8 bars' },
				{ value: '16', label: '16 bars' },
				{ value: '32', label: '32 bars' },
				{ value: '64', label: '64 bars' },
				{ value: 'whole', label: 'Whole track' }
			]
		}
	},
	{
		id: 'wave_zoom_playhead',
		label: 'Zoomed-out playhead',
		group: 'performance',
		keywords: ['waveform', 'zoom', 'playhead', 'elastic'],
		title: 'Where the deck playheads sit when the waveform is zoomed far out',
		detail:
			'Elastic: centered until the view passes half the master track, then glides so it all fits. IPC: set_wave_zoom_options.',
		implemented: true,
		control: {
			kind: 'enum',
			options: [
				{ value: 'elastic', label: 'Elastic (glide to fit)' },
				{ value: 'center', label: 'Always centered' }
			]
		}
	},
	{
		id: 'wave_zoom_linked',
		label: 'One zoom for all decks',
		group: 'performance',
		keywords: ['waveform', 'zoom', 'linked', 'deck'],
		title: 'Zoom every deck waveform together, or each deck on its own',
		detail:
			'Off: zoom only the deck under the pointer. IPC: set_wave_zoom_options.',
		implemented: true,
		control: { kind: 'boolean' }
	}
];
