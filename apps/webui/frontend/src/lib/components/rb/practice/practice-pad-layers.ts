/** Named pad layers are wire inspection state, never engine pad-mode commands (#12072). */
import type { DeviceMap } from '$lib/rb/midi/midi-types';
import {
	controlTitle,
	resolveLayout,
	validatePadLayers,
	type PadLayerSelection,
	type PanelLayout
} from './practice-bindings';

export interface PadLayerCommand {
	type: 'select';
	group: string;
	mode: string;
}

export function parsePadLayerCommand(input: unknown): PadLayerCommand {
	if (
		typeof input !== 'object' ||
		input === null ||
		!('type' in input) ||
		input.type !== 'select' ||
		!('group' in input) ||
		typeof input.group !== 'string' ||
		!('mode' in input) ||
		typeof input.mode !== 'string' ||
		Object.keys(input).some((key) => !['type', 'group', 'mode'].includes(key))
	)
		throw new TypeError('pad layer command expects { type: select, group, mode }');
	return { type: 'select', group: input.group, mode: input.mode };
}

export function initialPadLayers(layout: PanelLayout): PadLayerSelection {
	validatePadLayers(layout);
	return Object.fromEntries(
		Object.entries(layout.pad_layer_groups ?? {}).map(([id, group]) => [id, group.default_mode])
	);
}

export function selectPadLayer(
	layout: PanelLayout,
	selection: PadLayerSelection,
	input: unknown
): PadLayerSelection {
	const cmd = parsePadLayerCommand(input);
	const next = { ...selection, [cmd.group]: cmd.mode };
	validatePadLayers(layout, next);
	return next;
}

export function queryPadLayers(
	layout: PanelLayout,
	map: DeviceMap,
	selection: PadLayerSelection,
	shift: boolean
) {
	const resolved = resolveLayout(map, layout, shift, selection);
	return {
		device: layout.device,
		shift,
		groups: Object.fromEntries(
			Object.entries(layout.pad_layer_groups ?? {}).map(([id, group]) => [
				id,
				{
					label: group.label,
					default_mode: group.default_mode,
					selected_mode: selection[id] ?? group.default_mode,
					modes: group.modes.map((mode) => ({ ...mode }))
				}
			])
		),
		pads: resolved
			.filter((r) => r.control.pad_layers !== undefined)
			.map((r) => ({
				id: r.control.id,
				group: r.control.pad_layer_group,
				mode:
					selection[r.control.pad_layer_group ?? ''] ??
					layout.pad_layer_groups?.[r.control.pad_layer_group ?? '']?.default_mode,
				wire: r.wire === null ? null : { ...r.wire },
				status: r.kind,
				title: controlTitle(r)
			}))
	};
}

export type PadLayerQuery = ReturnType<typeof queryPadLayers>;
