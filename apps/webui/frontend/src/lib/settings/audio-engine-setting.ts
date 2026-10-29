import type { SettingDef } from './catalog-types';

export const AUDIO_ENGINE_SETTING: SettingDef = {
	id: 'audio_engine',
	label: 'Audio engine',
	group: 'performance',
	keywords: ['engine', 'rust', 'odj-audio', 'web audio', 'output', 'latency', 'native'],
	title: 'Which engine plays the decks on the performance page',
	detail:
		'Web Audio plays in the page, as it always has. Rust engine (preview) plays through odj-audio, a native engine the app starts for you, with the same beatgrids the waveforms draw. Takes effect the next time the performance page loads, so a set in progress is never cut. If the new engine is not straight away, Restart to load engine reloads this page onto the one you picked. In the preview, key sync, slip, stems, safety loops, AutoPlay Next, the headphone cue bus and headphone devices refuse by name instead of playing; Beat Sync, quantize, hot cues and AutoPlay work as in Web Audio, and keylock needs an engine build that reports it. Recording the master mix needs Web Audio.',
	implemented: true,
	control: {
		kind: 'enum',
		options: [
			{ value: 'webaudio', label: 'Web Audio' },
			{ value: 'rust', label: 'Rust engine (preview)' }
		]
	}
};
