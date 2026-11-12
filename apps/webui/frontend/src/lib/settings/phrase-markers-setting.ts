import type { SettingDef } from './catalog-types';

/**
 * Phrase markers on the waveform have no toggle today (always drawn). SETSTUB-01
 * keeps this out of SETTINGS_CATALOG; waveform quick-settings resolves inert
 * copy from WAVE_QS_INERT_DEFS instead.
 */
export const PHRASE_MARKERS_SETTING: SettingDef = {
	id: 'phrase_markers',
	label: 'Phrase markers',
	group: 'performance',
	keywords: ['phrase', 'markers', 'intro', 'verse', 'chorus', 'structure'],
	title: 'Phrase markers on the scrolling waveform',
	detail: 'Phrase markers (intro, verse, chorus) are always drawn.',
	implemented: false,
	control: { kind: 'boolean' }
};
