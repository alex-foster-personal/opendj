/**
 * WAVEQS-8348b (issue #8343): what the top bar's waveform quick-settings popover
 * shows, as data. Every live row NAMES a Settings catalog entry, so the
 * popover renders that entry's own label, explanation and preview and writes
 * through the same `applySettingChange` path the Settings overlay uses. There
 * is no second copy of any setting here, and no new storage.
 *
 * Grouping mimics rekordbox Preferences > View > Waveform and Serato DJ Pro's
 * Display options (look, zoom, what the waveform paints, extra lanes), copied
 * rather than calibrated: it is a hygiene choice.
 *
 * Rows with no preference behind them today (the painter draws them always, or
 * the feature is not on main yet) are INERT rows: shown, labeled, disabled,
 * with the house PARITY-TODO title, never a control that does nothing.
 *
 * Requirements (mini-PRD):
 *   ✔︎ every live row resolves to exactly one catalog entry that is implemented.
 *     [if] a row id names no catalog entry, or an unimplemented one [then ⛔️]
 *   ✔︎ a live row is a boolean, enum or number catalog entry.
 *     [if] a row names a path_lines, link or multi_bool entry [then ⛔️]
 *   ✔︎ every live row id is a key applySettingChange accepts.
 *     [if] a popover toggle writes a key the Settings path refuses [then ⛔️]
 *   ✔︎ inert rows carry exactly the PARITY-TODO title.
 *     [if] an inert row reads as a working control [then ⛔️]
 */
import type { SettingDef } from './catalog-types';

/** The house title for a control with no implementation behind it. */
export const WAVE_QS_INERT_TITLE = 'not implemented - see PARITY-TODO';

export type WaveQsRow =
	{ kind: 'setting'; id: string } | { kind: 'inert'; id: string; label: string; detail: string };

export interface WaveQsGroup {
	id: 'look' | 'zoom' | 'markers' | 'lanes';
	label: string;
	rows: readonly WaveQsRow[];
}

function _inert(id: string, label: string, detail: string): WaveQsRow {
	return { kind: 'inert', id, label, detail };
}

export const WAVE_QS_GROUPS: readonly WaveQsGroup[] = [
	{
		id: 'look',
		label: 'Look',
		rows: [
			{ kind: 'setting', id: 'waveform_design' },
			{ kind: 'setting', id: 'wave_palette' },
			{ kind: 'setting', id: 'ui_skin' },
			{ kind: 'setting', id: 'wave_height' }
		]
	},
	{
		id: 'zoom',
		label: 'Zoom',
		rows: [
			{ kind: 'setting', id: 'wave_zoom' },
			{ kind: 'setting', id: 'wave_zoom_playhead' },
			{ kind: 'setting', id: 'wave_zoom_linked' }
		]
	},
	{
		id: 'markers',
		label: 'On the waveform',
		rows: [
			{ kind: 'setting', id: 'show_sections' },
			_inert(
				'phrase_markers',
				'Phrase markers',
				'Phrase markers (intro, verse, chorus) are always drawn.'
			),
			{ kind: 'setting', id: 'wave_grid_lines' },
			_inert('hot_cues', 'Hot cues', 'Hot cue markers are always drawn.'),
			_inert('memory_cues', 'Memory cues', 'Memory cue markers are always drawn.'),
			_inert('loop_cues', 'Loop cues', 'Saved loop bands are always drawn.'),
			{ kind: 'setting', id: 'show_vocal_lane' },
			_inert(
				'tempo_lane',
				'Tempo-change lane',
				'The tempo-change lane is set in Settings > Performance: Show tempo changes and Tempo curve scale.'
			)
		]
	},
	{
		id: 'lanes',
		label: 'Lanes and lyrics',
		rows: [
			{ kind: 'setting', id: 'show_stems' },
			{ kind: 'setting', id: 'lyrics_waveform_overlay' },
			{ kind: 'setting', id: 'lyrics_hover_scrub' },
			{ kind: 'setting', id: 'wave_split_master' },
			{ kind: 'setting', id: 'jog_radial_waveform' }
		]
	}
];

/** The hover title for a live row: the setting's summary, then its agent twin. */
export function waveQsRowTitle(def: SettingDef): string {
	const verb = WAVE_QS_VERBS[def.id];
	if (verb === undefined)
		throw new Error(`waveform quick settings: ${def.id} has no command-bus verb`);
	return `${def.title}. Agents: opendj ${verb}`;
}

/** Resolve a live row to its catalog entry; a missing or unimplemented entry throws. */
export function resolveWaveQsSetting(catalog: readonly SettingDef[], id: string): SettingDef {
	const def = catalog.find((entry) => entry.id === id);
	if (def === undefined)
		throw new Error(`waveform quick settings: no Settings catalog entry ${id}`);
	if (!def.implemented)
		throw new Error(`waveform quick settings: catalog entry ${id} is not implemented`);
	if (
		def.control.kind !== 'boolean' &&
		def.control.kind !== 'enum' &&
		def.control.kind !== 'number'
	) {
		throw new Error(
			`waveform quick settings: ${id} is a ${def.control.kind} control, only boolean, enum and number render here`
		);
	}
	return def;
}

/**
 * WAVEQS-8348e agent parity: the command-bus verb (`opendj <verb>`, and POST
 * /api/v1/performance/commands) that sets each live row's preference. Every
 * live row must appear here; the unit test also checks each verb exists in the
 * CLI catalog. The popover names the verb in each row's hover title.
 */
export const WAVE_QS_VERBS: Readonly<Record<string, string>> = {
	waveform_design: 'set_waveform_design',
	wave_height: 'set_waveform_design',
	wave_palette: 'set_skin',
	ui_skin: 'set_skin',
	wave_split_master: 'set_skin',
	wave_zoom: 'set_wave_zoom',
	wave_zoom_playhead: 'set_wave_zoom_options',
	wave_zoom_linked: 'set_wave_zoom_options',
	show_stems: 'show_stems',
	show_sections: 'set_waveform_overlay',
	show_vocal_lane: 'set_waveform_overlay',
	lyrics_waveform_overlay: 'set_waveform_overlay',
	lyrics_hover_scrub: 'set_waveform_overlay',
	jog_radial_waveform: 'set_waveform_overlay',
	wave_grid_lines: 'set_waveform_overlay'
};
