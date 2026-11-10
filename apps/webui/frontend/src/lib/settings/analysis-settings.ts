import type { SettingDef } from './catalog-types';

const SHARED_ANALYZER_DETAIL =
	'Runs in the same analyzer pass as the other grid/key/sections lanes; the pass runs when any of them is on. Manual per-track orders ignore this toggle. Also via PUT /api/v1/ui-prefs.';

export const AUTO_ANALYZE_SETTINGS: SettingDef[] = [
	{
		id: 'auto_analyze_grid',
		label: 'Analyze grid on import',
		group: 'analysis',
		keywords: ['analyze', 'analyse', 'import', 'auto', 'bpm', 'beatgrid', 'grid'],
		title: 'Run beatgrid analysis automatically on newly imported tracks',
		detail: `Default on. ${SHARED_ANALYZER_DETAIL} Example: PUT /api/v1/ui-prefs {"auto_analyze_grid": true}.`,
		implemented: true,
		control: { kind: 'boolean' }
	},
	{
		id: 'auto_analyze_key',
		label: 'Analyze key on import',
		group: 'analysis',
		keywords: ['analyze', 'analyse', 'import', 'auto', 'key', 'camelot'],
		title: 'Run key analysis automatically on newly imported tracks',
		detail: `Default on. ${SHARED_ANALYZER_DETAIL}`,
		implemented: true,
		control: { kind: 'boolean' }
	},
	{
		id: 'auto_analyze_sections',
		label: 'Analyze sections on import',
		group: 'analysis',
		keywords: ['analyze', 'analyse', 'import', 'auto', 'phrase', 'sections', 'structure'],
		title: 'Run section/phrase analysis automatically on newly imported tracks',
		detail: `Default on. ${SHARED_ANALYZER_DETAIL}`,
		implemented: true,
		control: { kind: 'boolean' }
	},
	{
		id: 'auto_analyze_vocals',
		label: 'Analyze vocals on import',
		group: 'analysis',
		keywords: ['analyze', 'analyse', 'import', 'auto', 'vocal', 'vocals'],
		title: 'Fill vocal cache automatically when stems exist (slow lane)',
		detail:
			'Default off. Derived from stem bundles only; import never makes stems. Manual per-track vocals orders ignore this toggle. PUT /api/v1/ui-prefs {"auto_analyze_vocals": true}.',
		implemented: true,
		control: { kind: 'boolean' }
	}
];
