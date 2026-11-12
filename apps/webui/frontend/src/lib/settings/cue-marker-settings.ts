import type { SettingDef } from './catalog-types';

// Issue #10248. Split out of catalog.ts, which sits at the frontend file-size gate.
export const CUE_MARKER_SETTINGS: readonly SettingDef[] = [
	{
		id: 'show_hot_cue_markers',
		label: 'Show hot cue markers',
		group: 'performance',
		keywords: ['hot cue', 'hot cues', 'cue', 'marker', 'markers', 'waveform', 'overlay', 'pad'],
		title: 'Paint hot cue markers on the deck waveforms and overview strip',
		detail:
			'When on (default), non-loop hot cue markers paint on the deck waveforms and overview strip. Also settable with PUT /api/v1/ui-prefs (show_hot_cue_markers) and `opendj set_waveform_overlay hot_cues false`. A hidden marker is not painted and not clickable.',
		implemented: true,
		control: { kind: 'boolean' }
	},
	{
		id: 'show_memory_cue_markers',
		label: 'Show memory cue markers',
		group: 'performance',
		keywords: ['memory cue', 'memory cues', 'cue', 'marker', 'markers', 'waveform', 'overlay'],
		title: 'Paint memory cue markers on the deck waveforms and overview strip',
		detail:
			'When on (default), memory cue markers paint on the deck waveforms and overview strip. Also settable with PUT /api/v1/ui-prefs (show_memory_cue_markers) and `opendj set_waveform_overlay memory_cues false`. A hidden marker is not painted and not clickable.',
		implemented: true,
		control: { kind: 'boolean' }
	},
	{
		id: 'show_loop_markers',
		label: 'Show loop markers',
		group: 'performance',
		keywords: ['loop', 'loop cue', 'loop cues', 'cue', 'marker', 'markers', 'waveform', 'overlay'],
		title:
			'Paint stored loop bands and the engaged-loop band on deck waveforms and the overview strip',
		detail:
			'When on (default), saved loop cue bands and the engaged-loop region paint on the deck waveforms and overview strip. Also settable with PUT /api/v1/ui-prefs (show_loop_markers) and `opendj set_waveform_overlay loop_cues false`. A hidden marker is not painted and not clickable.',
		implemented: true,
		control: { kind: 'boolean' }
	}
];
