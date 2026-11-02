import type { SettingDef } from './catalog-types';

export const PREVIEW_BEAT_SYNC_SETTING: SettingDef = {
	id: 'preview_beat_sync',
	label: 'Beat Sync Max: library previews',
	group: 'performance',
	keywords: ['beatsync', 'preview', 'cue', 'tempo', 'pitch', 'headphones', 'library', 'phase', 'snap', 'bar', 'downbeat'],
	title: 'Tempo-match a library preview to the playing master deck',
	detail:
		'Snap to bar starts a preview on the clicked bar, in phase with the master deck, so downbeats land on downbeats. Tempo match plays from exactly where you click at the master tempo. Both fit the preview pitch range (half and double time count); pitch moves with tempo. Off plays every preview at its own tempo.',
	implemented: true,
	control: {
		kind: 'enum',
		options: [
			{ value: 'phase', label: 'Snap to bar, in phase with master' },
			{ value: 'tempo', label: 'Tempo match master' },
			{ value: 'off', label: 'Off (own tempo)' }
		]
	}
};
