import type { SettingDef } from './catalog-types';

export const MIDI_ENABLED_SETTING: SettingDef = {
	id: 'rb.midi_enabled',
	label: 'MIDI controllers',
	group: 'performance',
	keywords: ['midi', 'controller', 'webmidi', 'ddj', 'device'],
	title: 'Enable WebMIDI controller input',
	detail:
		'When on, the app requests browser MIDI access on load and routes mapped controllers to decks and mixer. Persisted to ui-prefs (PARITY-12).',
	implemented: true,
	control: { kind: 'boolean' }
};
