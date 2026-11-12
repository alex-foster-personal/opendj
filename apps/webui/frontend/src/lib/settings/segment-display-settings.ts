import type { SettingDef } from './catalog-types';

/** STRUCTUI-12787 (issue #11436). Split out of catalog.ts for the file-size gate. */
export const SEGMENT_DISPLAY_SETTINGS: readonly SettingDef[] = [
	{
		id: 'sections_main_style',
		label: 'Main waveform style',
		group: 'performance',
		keywords: [
			'section',
			'sections',
			'segment',
			'segments',
			'structure',
			'waveform',
			'tint',
			'recolor',
			'boundary',
			'base line',
			'main'
		],
		title: 'How the main scrolling waveform shows song sections',
		detail:
			'Tint + labels (default) draws a faint tint per section, a chip at each change, and a sticky title for the section under the playhead. Recolor waveform tints each band by section hue. Boundary lines draws colored start lines and short tags. Base line draws a section-colored line under the waveform with labels on the bottom edge. Also settable with PUT /api/v1/ui-prefs and opendj set_segment_display.',
		implemented: true,
		preview: 'sections',
		control: {
			kind: 'enum',
			options: [
				{ value: 'm1_tint', label: 'Tint + labels (default)' },
				{ value: 'm2_recolor', label: 'Recolor waveform' },
				{ value: 'm3_lines', label: 'Boundary lines' },
				{ value: 'm4_base', label: 'Base line' }
			]
		}
	},
	{
		id: 'sections_sticky_title',
		label: 'Sticky section title',
		group: 'performance',
		keywords: ['section', 'sticky', 'title', 'label', 'main', 'waveform'],
		title: 'Pin the active section name on the main waveform',
		detail:
			'When on (default), the section name under the playhead stays pinned to the left edge of the main waveform and changes as the playhead crosses each section. Also settable with PUT /api/v1/ui-prefs and opendj set_segment_display.',
		implemented: true,
		preview: 'sections',
		control: { kind: 'boolean' }
	},
	{
		id: 'sections_next_countdown',
		label: 'Next-section countdown',
		group: 'performance',
		keywords: ['section', 'countdown', 'next', 'bars', 'drop', 'waveform'],
		title: 'Show bars until the next section change beside the playhead',
		detail:
			'When on (default), the main waveform shows a countdown such as "DROP in 8 bars" for the next section boundary. Also settable with PUT /api/v1/ui-prefs and opendj set_segment_display.',
		implemented: true,
		preview: 'sections',
		control: { kind: 'boolean' }
	},
	{
		id: 'sections_overview_style',
		label: 'Deck overview style',
		group: 'performance',
		keywords: ['section', 'overview', 'strip', 'deck', 'block', 'tint', 'recolor'],
		title: 'How the deck overview strip shows song sections',
		detail:
			'Block strip + pins (default) draws section blocks above the overview with drop pins; clicking a block jumps to its first bar. Tint tints the overview background per section. Recolor draws the overview in section colors. Also settable with PUT /api/v1/ui-prefs and opendj set_segment_display.',
		implemented: true,
		preview: 'sections',
		control: {
			kind: 'enum',
			options: [
				{ value: 'o1_strip', label: 'Block strip + pins (default)' },
				{ value: 'o2_tint', label: 'Tint' },
				{ value: 'o3_recolor', label: 'Recolor' }
			]
		}
	},
	{
		id: 'sections_library_structure_column',
		label: 'Structure column',
		group: 'library',
		keywords: ['section', 'structure', 'library', 'column', 'drop', 'bars'],
		title: 'Structure bar column in the track list',
		detail:
			'When on (default), each library row shows the track shape in blocks plus a sortable bars-to-first-drop number. Also settable with PUT /api/v1/ui-prefs and opendj set_segment_display.',
		implemented: true,
		preview: 'sections',
		control: { kind: 'boolean' }
	},
	{
		id: 'sections_preview_cursor_label',
		label: 'Label under cursor',
		group: 'library',
		keywords: ['section', 'preview', 'library', 'cursor', 'hover', 'scrub'],
		title: 'Section label under the library preview cursor',
		detail:
			'When on (default), hovering or scrubbing the library preview waveform lights the section under the cursor and shows its name and bar. Also settable with PUT /api/v1/ui-prefs and opendj set_segment_display.',
		implemented: true,
		preview: 'sections',
		control: { kind: 'boolean' }
	},
	{
		id: 'sections_uncertain',
		label: 'Uncertain sections',
		group: 'performance',
		keywords: ['section', 'uncertain', 'confidence', 'hide', 'faded', 'question'],
		title: 'Whether low-confidence sections appear in the UI',
		detail:
			'Hide (default) drops sections below the #6480 confidence cutoff from labels and lists. Show faded, labels marked ? keeps them with a faded hatch and a question mark on every text label. Also settable with PUT /api/v1/ui-prefs and opendj set_segment_display.',
		implemented: true,
		preview: 'sections',
		control: {
			kind: 'enum',
			options: [
				{ value: 'hide', label: 'Hide (default)' },
				{ value: 'show_faded', label: 'Show faded, labels marked ?' }
			]
		}
	}
];
