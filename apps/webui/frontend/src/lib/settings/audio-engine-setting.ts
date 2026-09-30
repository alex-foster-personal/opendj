import type { SettingDef } from './catalog-types';

export const AUDIO_ENGINE_SETTING: SettingDef = {
	id: 'audio_engine',
	label: 'Audio engine',
	group: 'performance',
	keywords: ['engine', 'rust', 'odj-audio', 'web audio', 'output', 'latency', 'native'],
	title: 'Which engine plays the decks on the performance page',
	detail:
		'Web Audio plays in the page, as it always has. Rust engine (preview) plays through odj-audio, a native engine the app starts for you, with the same beatgrids the waveforms draw. Takes effect the next time the performance page loads, so a set in progress is never cut. In the preview, sync, keylock, stems, hot-cue save, safety loops, headphone devices and auto-play refuse by name instead of playing; everything else works.',
	implemented: true,
	control: {
		kind: 'enum',
		options: [
			{ value: 'webaudio', label: 'Web Audio' },
			{ value: 'rust', label: 'Rust engine (preview)' }
		]
	}
};
