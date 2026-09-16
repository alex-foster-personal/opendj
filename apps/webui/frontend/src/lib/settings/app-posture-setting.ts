import type { SettingDef } from './catalog-types';

export const APP_POSTURE_SETTING: SettingDef = {
	id: 'app_posture',
	label: 'Resource posture',
	group: 'performance',
	keywords: ['gig', 'prep', 'posture', 'practice', 'live set', 'poll', 'prefetch', 'workers'],
	title: 'Resource posture: Prep (practice background work) or Gig (live-set caps). Not the app-mode chooser.',
	detail:
		'Prep is the default. Gig lengthens library fallback poll to 5 minutes, halves background vocals workers, and floors audio prefetch at 2 tracks / 24 MiB. This is not the Gig card that opens /performance.',
	implemented: true,
	control: {
		kind: 'enum',
		options: [
			{ value: 'prep', label: 'Prep' },
			{ value: 'gig', label: 'Gig' }
		]
	}
};
