/**
 * Practice replica bindings: join a panel layout control to the SAME DeviceMap the real
 * controller drives (ADR-0137). The layout stores only the wire identity a control sends
 * (channel, note|cc, number); the action usually comes from the map, so an on-screen press
 * and a hardware press resolve through one table and cannot disagree. For a few FLX4 wires
 * the map lists as hints only while Open DJ already has a matching MidiAction (issue #9405,
 * SKIN-9564), the replica adds a local supplement below so practice can fire that action;
 * a real FLX4 press on those wires still hits `unmapped` until FLX4_MAP binds them.
 *
 * Requirements (mini-PRD):
 *   ✔︎ resolveControl returns the map's binding for the control's wire source.
 *     [if] the FLX4 replica's deck 1 PLAY resolves to anything but deck_play_toggle deck 1 [then] ⛔️
 *   ✔︎ A control whose source is only a map HINT resolves to kind 'inert' carrying the hint
 *     label, and renders inert with the PARITY-TODO tooltip (house rule: no fake behavior).
 *     [if] a hint-only control with no supplement row (JOG, BEAT JUMP, PAD FX, SAMPLER, FX) resolves bound [then] ⛔️
 *     [if] BEAT SYNC on deck N does not fire deck_sync_toggle deck N [then] ⛔️
 *   ✔︎ A control the map neither binds nor hints throws: the layout and map disagree.
 *     [if] a layout control with a wire number absent from the map resolves silently [then] ⛔️
 *   ✔︎ A control whose midi is null (wire unavailable) resolves to kind 'unwired' and renders inert with the PARITY-TODO title; no number is invented to satisfy the map.
 *     [if] a null-wire control resolves bound, or a present wire the map lacks stops throwing [then] ⛔️
 *   ✔︎ controlIdForWire maps a wire (base or SHIFT row) back to the panel control that sends it (#10251).
 *     [if] any FLX4 control's wire does not map back to its own id [then] ⛔️
 *     [if] a wire the panel does not list maps to a control [then] ⛔️
 *     [if] two controls claiming one wire resolve silently [then] ⛔️
 */

import type {
	DeviceMap,
	MidiAction,
	MidiBinding,
	MidiInputValue,
	MidiSourceKind
} from '$lib/rb/midi/midi-types';

export interface PanelWire {
	ch: number;
	kind: MidiSourceKind;
	id: number;
}

export type PanelControlType =
	'jog' | 'fader' | 'knob' | 'pad' | 'button' | 'crossfader' | 'encoder';

export interface PadLayer {
	label: string;
	does: string;
	midi: PanelWire | null;
	shift_midi: PanelWire | null;
}

export interface PadLayerGroup {
	label: string;
	default_mode: string;
	modes: { id: string; label: string }[];
}

export type PadLayerSelection = Readonly<Record<string, string>>;

export interface PanelControl {
	id: string;
	fig: string;
	type: PanelControlType;
	label: string;
	does: string;
	x: number;
	y: number;
	w: number;
	h: number;
	section: string;
	/** What the control sends; null = the maker publishes no wire identity (unwired, never invented). */
	midi: PanelWire | null;
	/** What the control sends while SHIFT is held; null = no documented shifted row. */
	shift_midi: PanelWire | null;
	pad_layer_group?: string;
	pad_layers?: Record<string, PadLayer>;
	pad_layer_select?: { base: string; shift: string };
}

export interface PanelLayout {
	format: 'opendj-panel-layout/1';
	device: string;
	display_name: string;
	units: 'mm';
	panel: { w: number; d: number };
	device_map: string;
	controls: PanelControl[];
	pad_layer_groups?: Record<string, PadLayerGroup>;
}

/** Must match `FLX4_MAP.nameMatch` in `apps/webui/frontend/src/lib/rb/midi/maps/ddj-flx4.ts`. */
const _FLX4_NAME_MATCH = 'DDJ-FLX4';

/**
 * Replica-only bindings for FLX4 wires that FLX4_MAP documents as hints only (#9405); changing
 * the shipped map is out of scope, while Pioneer-style maps elsewhere already bind the same
 * actions to these wires. PAD FX and SAMPLER are omitted: `instant_fx` and `sampler` pad modes are
 * not available yet (`selectControllerPadMode`), and replica performance pads still fire
 * `deck_hot_cue` regardless of pad mode, so binding those wires would look functional while pad
 * presses still seek hot cues (review PRRT_kwDOSEvNd860tQ87).
 */
export const PRACTICE_WIRE_BINDINGS: readonly MidiBinding[] = (() => {
	const rows: MidiBinding[] = [];
	const note = (ch: 1 | 2, id: number, action: MidiAction): void => {
		rows.push({ source: { ch, kind: 'note', id }, action });
	};
	for (const deck of [1, 2] as const) {
		const ch = deck;
		note(ch, 88, { type: 'deck_sync_toggle', deck });
		note(ch, 16, { type: 'deck_manual_loop_cycle', deck });
		note(ch, 17, { type: 'deck_manual_loop_cycle', deck });
		note(ch, 27, { type: 'controller_pad_mode', deck, mode: 'hot_cue' });
	}
	return rows;
})();

/** `wire` is what the control sends on the resolved layer (base, or SHIFT's row). */
export type ResolvedControl =
	| { kind: 'bound'; control: PanelControl; wire: PanelWire; binding: MidiBinding }
	| { kind: 'inert'; control: PanelControl; wire: PanelWire; hint: string }
	| { kind: 'unwired'; control: PanelControl; wire: null };

function _sameSource(a: PanelWire, b: PanelWire): boolean {
	return a.ch === b.ch && a.kind === b.kind && a.id === b.id;
}

function _practiceSupplementBinding(map: DeviceMap, wire: PanelWire): MidiBinding | undefined {
	if (map.nameMatch !== _FLX4_NAME_MATCH) return undefined;
	return PRACTICE_WIRE_BINDINGS.find((b) => _sameSource(wire, b.source));
}

/** Join one panel control to the device map on the active layer. Like the hardware, a held
 * SHIFT changes what the control SENDS (its shifted row) and the map decides what that does:
 * a shifted binding first, then an unshifted one on the same source, else the hint (inert). */
export function resolveControl(
	map: DeviceMap,
	control: PanelControl,
	shiftHeld = false,
	selection: PadLayerSelection = {},
	layout?: PanelLayout
): ResolvedControl {
	if (layout === undefined && Object.keys(selection).length > 0)
		throw new Error('pad layer selection requires its panel layout');
	let namedLayer = false;
	if (control.pad_layers !== undefined && layout !== undefined) {
		const groupId = control.pad_layer_group;
		const group = groupId === undefined ? undefined : layout.pad_layer_groups?.[groupId];
		if (group === undefined || groupId === undefined)
			throw new Error(`unknown pad layer group on ${control.id}`);
		const mode = selection[groupId] ?? group.default_mode;
		const layer = control.pad_layers[mode];
		if (layer === undefined || !group.modes.some((m) => m.id === mode))
			throw new Error(`unknown pad layer ${mode} on ${control.id}`);
		control = { ...control, ...layer, label: `${layer.label}${shiftHeld ? ' +SHIFT' : ''}` };
		namedLayer = true;
	}
	if (namedLayer && (shiftHeld ? control.shift_midi : control.midi) === null)
		return { kind: 'unwired', control, wire: null };
	if (!namedLayer && control.midi === null) {
		if (control.shift_midi !== null)
			throw new Error(`practice layout ${control.id} has a shifted row but no base wire`);
		return { kind: 'unwired', control, wire: null };
	}
	const wire = shiftHeld && control.shift_midi !== null ? control.shift_midi : control.midi;
	if (wire === null) throw new Error(`practice layout ${control.id} has no selected wire`);
	const onWire = map.bindings.filter((b) => _sameSource(wire, b.source));
	const binding =
		(shiftHeld ? onWire.find((b) => b.shift === true) : undefined) ??
		onWire.find((b) => b.shift !== true);
	if (binding !== undefined) return { kind: 'bound', control, wire, binding };
	const supplement = _practiceSupplementBinding(map, wire);
	if (supplement !== undefined) return { kind: 'bound', control, wire, binding: supplement };
	const hint = (map.hints ?? []).find((h) => _sameSource(wire, h.source));
	if (hint !== undefined) return { kind: 'inert', control, wire, hint: hint.label };
	const { ch, kind, id } = wire;
	throw new Error(
		`practice layout ${control.id} sends ch ${ch} ${kind} ${id}, which ${map.nameMatch} neither binds nor hints`
	);
}

/** Validate cross-references before any inspection command changes state. */
export function validatePadLayers(layout: PanelLayout, selection: PadLayerSelection = {}): void {
	const groups = layout.pad_layer_groups ?? {};
	for (const [id, group] of Object.entries(groups)) {
		if (
			!group.label ||
			!Array.isArray(group.modes) ||
			group.modes.length === 0 ||
			group.modes.some((m) => !m.id || !m.label) ||
			new Set(group.modes.map((m) => m.id)).size !== group.modes.length ||
			!group.modes.some((m) => m.id === group.default_mode)
		)
			throw new Error(`invalid pad layer group ${id}`);
	}
	for (const [id, mode] of Object.entries(selection)) {
		if (!Object.hasOwn(groups, id) || !groups[id].modes.some((m) => m.id === mode))
			throw new Error(`unknown pad layer selection ${id}: ${mode}`);
	}
	for (const c of layout.controls) {
		if (
			c.pad_layer_group === undefined &&
			c.pad_layers === undefined &&
			c.pad_layer_select === undefined
		)
			continue;
		const group = c.pad_layer_group === undefined ? undefined : groups[c.pad_layer_group];
		if (group === undefined || (c.pad_layers === undefined) === (c.pad_layer_select === undefined))
			throw new Error(`invalid pad layer metadata on ${c.id}`);
		if (c.pad_layers !== undefined) {
			if (c.type !== 'pad' || Object.keys(c.pad_layers).length !== group.modes.length)
				throw new Error(`incomplete pad layers on ${c.id}`);
			for (const mode of group.modes) {
				const layer = c.pad_layers[mode.id];
				if (
					!layer ||
					!layer.label ||
					!layer.does ||
					!Object.hasOwn(layer, 'midi') ||
					!Object.hasOwn(layer, 'shift_midi') ||
					layer.midi === undefined ||
					layer.shift_midi === undefined ||
					Object.keys(layer).some((k) => !['label', 'does', 'midi', 'shift_midi'].includes(k))
				)
					throw new Error(`invalid pad layer ${mode.id} on ${c.id}`);
			}
		} else if (c.pad_layer_select !== undefined) {
			if (
				c.type !== 'button' ||
				!group.modes.some((m) => m.id === c.pad_layer_select?.base) ||
				!group.modes.some((m) => m.id === c.pad_layer_select?.shift)
			)
				throw new Error(`invalid pad layer selector on ${c.id}`);
		}
	}
}

/** Inverse of resolveControl's wire pick: which panel control sends this wire (base or SHIFT
 * row). Throws when two controls claim one wire; null when the panel lists none. */
export function controlIdForWire(layout: PanelLayout, wire: PanelWire): string | null {
	const claimed = layout.controls.filter(
		(c) =>
			(c.midi !== null && _sameSource(c.midi, wire)) ||
			(c.shift_midi !== null && _sameSource(c.shift_midi, wire))
	);
	if (claimed.length > 1) {
		const { ch, kind, id } = wire;
		throw new Error(
			`practice layout ${layout.device}: wire ch ${ch} ${kind} ${id} is claimed by ${claimed[0].id} and ${claimed[1].id}`
		);
	}
	return claimed.length === 1 ? claimed[0].id : null;
}

export function resolveLayout(
	map: DeviceMap,
	layout: PanelLayout,
	shiftHeld = false,
	selection: PadLayerSelection = {}
): ResolvedControl[] {
	if (layout.format !== 'opendj-panel-layout/1') {
		throw new Error(`unsupported panel layout format: ${String(layout.format)}`);
	}
	validatePadLayers(layout, selection);
	return layout.controls.map((c) => resolveControl(map, c, shiftHeld, selection, layout));
}

/** The value a hardware message of this control would carry, from a 0..1 position or press. */
export function inputValueFor(
	binding: MidiBinding,
	input: { pressed: boolean } | { value01: number } | { delta: number }
): MidiInputValue {
	if ('pressed' in input)
		return { kind: 'button', pressed: input.pressed, velocity: input.pressed ? 127 : 0 };
	if ('delta' in input) return { kind: 'relative', delta: input.delta };
	const v = Math.min(1, Math.max(0, input.value01));
	if (binding.action.type === 'deck_pitch' && binding.action.lsbOffset !== null) {
		return { kind: 'continuous14', value01: v, raw: Math.round(v * 16383) };
	}
	return { kind: 'continuous', value01: v, raw: Math.round(v * 127) };
}

/** Plain-words hover text: what the control does, and what it fires in Open DJ. */
export function controlTitle(resolved: ResolvedControl): string {
	const identity =
		resolved.wire === null
			? 'wire unavailable'
			: `ch ${resolved.wire.ch} ${resolved.wire.kind} ${resolved.wire.id}`;
	const base = `${resolved.control.label}: ${resolved.control.does} (${identity})`;
	if (resolved.kind === 'unwired')
		return `${base}. not implemented - see PARITY-TODO (selected wire unavailable: no MIDI identity published)`;
	if (resolved.kind === 'inert')
		return `${base}. not implemented - see PARITY-TODO (${resolved.hint})`;
	return `${base}. Fires ${describeAction(resolved.binding.action)}`;
}

export function describeAction(action: MidiAction): string {
	const parts = Object.entries(action)
		.filter(([k]) => k !== 'type')
		.map(([k, v]) => `${k} ${String(v)}`);
	return parts.length === 0 ? action.type : `${action.type} (${parts.join(', ')})`;
}

/** Where a replica dispatch goes. `action` is the glue's action switch; `shift` is the modifier
 * state the hardware path keeps in webmidi (midiState.shiftHeld), which the glue never sees. */
export interface PracticeSinks {
	action: (action: MidiAction, value: MidiInputValue, controlId: string) => void;
	shift: (held: boolean) => void;
	log?: (line: string) => void;
}

export type PracticeDispatch = 'inert' | 'modifier' | 'action';

/** Route one replica input exactly like the hardware path: SHIFT updates modifier state and
 * never reaches the action glue (which throws on shift_modifier); everything else goes to it. */
export function dispatchPractice(
	resolved: ResolvedControl,
	input: { pressed: boolean } | { value01: number } | { delta: number },
	sinks: PracticeSinks
): PracticeDispatch {
	if (resolved.kind !== 'bound') return 'inert';
	const value = inputValueFor(resolved.binding, input);
	if (resolved.binding.action.type === 'shift_modifier') {
		if (value.kind !== 'button') throw new Error('shift_modifier must be a button control');
		sinks.shift(value.pressed);
		return 'modifier';
	}
	const { ch, kind, id } = resolved.wire;
	sinks.log?.(
		`[practice] ${resolved.control.id} ch ${ch} ${kind} ${id} -> ${describeAction(resolved.binding.action)} ${JSON.stringify(value)}`
	);
	sinks.action(resolved.binding.action, value, resolved.control.id);
	return 'action';
}

/** Own press snapshots so layer changes, cancel, lost capture and teardown release once. */
export function createPracticeHolds(sinks: PracticeSinks) {
	const held = new Map<string, ResolvedControl>();
	const release = (id: string): void => {
		const resolved = held.get(id);
		if (resolved === undefined) return;
		held.delete(id);
		dispatchPractice(resolved, { pressed: false }, sinks);
	};
	return {
		press(resolved: ResolvedControl): void {
			if (held.has(resolved.control.id)) return;
			held.set(resolved.control.id, resolved);
			dispatchPractice(resolved, { pressed: true }, sinks);
		},
		release,
		releaseAll(): void {
			for (const id of [...held.keys()]) release(id);
		}
	};
}
