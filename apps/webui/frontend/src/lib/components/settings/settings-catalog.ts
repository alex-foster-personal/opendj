/**
 * WAVEQS-8348b (issue #8343, #12518): waveform quick-settings screen registry.
 * Group order, live and inert row ids, and resolvers. Live rows bind
 * SETTINGS_CATALOG; inert rows bind WAVE_QS_INERT_DEFS plus composite rules.
 */
import { PHRASE_MARKERS_SETTING } from '$lib/settings/phrase-markers-setting';
import type { SettingDef } from '$lib/settings/catalog-types';

/** The house title for a control with no implementation behind it. */
export const WAVE_QS_INERT_TITLE = 'not implemented - see PARITY-TODO';

/** Inert quick-settings ids whose copy lives outside the shipped catalog (SETSTUB-01). */
export const WAVE_QS_INERT_DEFS: readonly SettingDef[] = [PHRASE_MARKERS_SETTING];

export type WaveQsRow = { kind: 'setting'; id: string } | { kind: 'inert'; id: string };

export interface WaveQsGroup {
	id: 'look' | 'zoom' | 'markers' | 'lanes';
	label: string;
	rows: readonly WaveQsRow[];
}

function _inert(id: string): WaveQsRow {
	return { kind: 'inert', id };
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
			_inert('phrase_markers'),
			{ kind: 'setting', id: 'wave_grid_lines' },
			{ kind: 'setting', id: 'show_hot_cue_markers' },
			{ kind: 'setting', id: 'show_memory_cue_markers' },
			{ kind: 'setting', id: 'show_loop_markers' },
			{ kind: 'setting', id: 'show_vocal_lane' },
			_inert('tempo_lane')
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

function _catalogEntry(catalog: readonly SettingDef[], id: string): SettingDef {
	const def = catalog.find((entry) => entry.id === id);
	if (def === undefined)
		throw new Error(`waveform quick settings: no Settings catalog entry ${id}`);
	return def;
}

function _inertDef(rowId: string): SettingDef {
	const def = WAVE_QS_INERT_DEFS.find((entry) => entry.id === rowId);
	if (def === undefined)
		throw new Error(`waveform quick settings: no inert definition for ${rowId}`);
	return def;
}

/** Labels and explanations for disabled quick-settings rows (WAVEQS-8348b, issue #12518). */
export function resolveWaveQsInertDisplay(
	catalog: readonly SettingDef[],
	rowId: string
): { label: string; detail: string } {
	if (rowId === 'phrase_markers') {
		const def = _inertDef(rowId);
		return { label: def.label, detail: def.detail };
	}
	if (rowId === 'tempo_lane') {
		const showTempo = _catalogEntry(catalog, 'show_tempo_lane');
		const scale = _catalogEntry(catalog, 'tempo_lane_scale');
		return {
			label: 'Tempo-change lane',
			detail: `The tempo-change lane is set in Settings > Performance: ${showTempo.label} and ${scale.label}.`
		};
	}
	throw new Error(`waveform quick settings: no inert display rule for ${rowId}`);
}

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
	wave_grid_lines: 'set_waveform_overlay',
	show_hot_cue_markers: 'set_waveform_overlay',
	show_memory_cue_markers: 'set_waveform_overlay',
	show_loop_markers: 'set_waveform_overlay'
};
