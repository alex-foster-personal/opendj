import type { SettingDef } from './catalog-types';

// AUDIO-DEVICE-06. Split out of catalog.ts, which sits at the frontend
// file-size gate, the same way show-sections-setting.ts is.
export const SPEAKER_AUTO_RECOVER_SETTING: SettingDef = {
	id: 'speaker_auto_recover',
	label: 'Auto-reconnect stalled speaker',
	group: 'performance',
	keywords: ['bluetooth', 'speaker', 'hyperboom', 'reconnect', 'stall', 'silent', 'no sound', 'output', 'recover'],
	title: 'When a Bluetooth main speaker stops playing, disconnect and reconnect it automatically',
	detail:
		'Auto (default) reconnects at home (Prep posture) and never in Gig posture. Always reconnects in both; Never leaves it to the Reconnect speaker button in I/O. At most one automatic reconnect per 60 s; a second stall inside that shows a prompt instead. Each reconnect drops the speaker for about 5 s. Also settable with PUT /api/v1/ui-prefs (speaker_auto_recover).',
	implemented: true,
	control: {
		kind: 'enum',
		options: [
			{ value: 'auto', label: 'Auto (on at home, off in Gig)' },
			{ value: 'on', label: 'Always' },
			{ value: 'off', label: 'Never (use Reconnect speaker)' }
		]
	}
};
