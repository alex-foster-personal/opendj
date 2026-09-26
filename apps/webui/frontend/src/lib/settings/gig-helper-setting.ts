import type { SettingDef } from './catalog-types';

export const GIG_HELPER_SETTING: SettingDef = {
	id: 'gig_helper',
	label: 'Gig helper',
	group: 'performance',
	keywords: ['gig', 'helper', 'pressure', 'monitor', 'posture', 'load'],
	title: 'Gig helper monitors system pressure during Gig posture and keeps background caps active.',
	detail:
		'Ask on first Gig opens an opt-in when you switch to Gig resource posture. On enables monitoring toasts; Off disables the helper until you turn it back on. Native menubar integration is planned separately.',
	implemented: true,
	control: {
		kind: 'enum',
		options: [
			{ value: 'unset', label: 'Ask on first Gig' },
			{ value: 'off', label: 'Off' },
			{ value: 'on', label: 'On' }
		]
	}
};
