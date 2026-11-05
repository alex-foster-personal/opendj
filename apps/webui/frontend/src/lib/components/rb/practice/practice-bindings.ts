/**
 * Practice replica bindings: join a panel layout control to the SAME DeviceMap the real
 * controller drives (ADR-0137). The layout stores only the wire identity a control sends
 * (channel, note|cc, number); the action comes from the map, so an on-screen press and a
 * hardware press resolve through one table and cannot disagree.
 *
 * Requirements (mini-PRD):
 *   ✔︎ resolveControl returns the map's binding for the control's wire source.
 *     [if] the FLX4 replica's deck 1 PLAY resolves to anything but deck_play_toggle deck 1 [then] ⛔️
 *   ✔︎ A control whose source is only a map HINT resolves to kind 'inert' carrying the hint
 *     label, and renders inert with the PARITY-TODO tooltip (house rule: no fake behavior).
 *     [if] JOG resolves to a bound action while the map only hints it [then] ⛔️
 *   ✔︎ A control the map neither binds nor hints throws: the layout and map disagree.
 *     [if] a layout control with a wire number absent from the map resolves silently [then] ⛔️
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
	midi: PanelWire;
	/** What the control sends while SHIFT is held; null = no documented shifted row. */
	shift_midi: PanelWire | null;
}

export interface PanelLayout {
	format: 'opendj-panel-layout/1';
	device: string;
	display_name: string;
	units: 'mm';
	panel: { w: number; d: number };
	device_map: string;
	controls: PanelControl[];
}

/** `wire` is what the control sends on the resolved layer (base, or SHIFT's row). */
export type ResolvedControl =
	| { kind: 'bound'; control: PanelControl; wire: PanelWire; binding: MidiBinding }
	| { kind: 'inert'; control: PanelControl; wire: PanelWire; hint: string };

function _sameSource(a: PanelWire, b: MidiBinding['source']): boolean {
	return a.ch === b.ch && a.kind === b.kind && a.id === b.id;
}

/** Join one panel control to the device map on the active layer. Like the hardware, a held
 * SHIFT changes what the control SENDS (its shifted row) and the map decides what that does:
 * a shifted binding first, then an unshifted one on the same source, else the hint (inert). */
export function resolveControl(
	map: DeviceMap,
	control: PanelControl,
	shiftHeld = false
): ResolvedControl {
	const wire = shiftHeld && control.shift_midi !== null ? control.shift_midi : control.midi;
	const onWire = map.bindings.filter((b) => _sameSource(wire, b.source));
	const binding =
		(shiftHeld ? onWire.find((b) => b.shift === true) : undefined) ??
		onWire.find((b) => b.shift !== true);
	if (binding !== undefined) return { kind: 'bound', control, wire, binding };
	const hint = (map.hints ?? []).find((h) => _sameSource(wire, h.source));
	if (hint !== undefined) return { kind: 'inert', control, wire, hint: hint.label };
	const { ch, kind, id } = wire;
	throw new Error(
		`practice layout ${control.id} sends ch ${ch} ${kind} ${id}, which ${map.nameMatch} neither binds nor hints`
	);
}

export function resolveLayout(map: DeviceMap, layout: PanelLayout): ResolvedControl[] {
	if (layout.format !== 'opendj-panel-layout/1') {
		throw new Error(`unsupported panel layout format: ${String(layout.format)}`);
	}
	return layout.controls.map((c) => resolveControl(map, c));
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
	const base = `${resolved.control.label}: ${resolved.control.does}`;
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
