import type { SettingDef } from './catalog-types';

export const PREVIEW_BEAT_SYNC_SETTING: SettingDef = {
	id: 'preview_beat_sync',
	label: 'Beat Sync Max: library previews',
	group: 'performance',
	keywords: ['beatsync', 'preview', 'cue', 'tempo', 'pitch', 'headphones', 'library'],
	title: 'Tempo-match a library preview to the playing master deck',
	detail:
		'Tempo match plays a preview at the master deck tempo when the match fits the preview pitch range; half and double time count as a match. The preview voice has no keylock, so pitch moves with tempo. Off plays every preview at its own tempo. Downbeat alignment is not built yet (CUEOUT-16).',
	implemented: true,
	control: {
		kind: 'enum',
		options: [
			{ value: 'tempo', label: 'Tempo match master' },
			{ value: 'off', label: 'Off (own tempo)' }
		]
	}
};
