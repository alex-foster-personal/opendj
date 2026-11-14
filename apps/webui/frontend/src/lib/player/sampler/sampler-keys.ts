/**
 * Edge sampler key policy (spec `(private path)` 7.3, 7.4; issue #13720).
 *
 * Existing bindings win. Digits 1 to 8 are the transport deck's hot cues
 * (#10354) and `[` / `]` zoom the main waveform (WAVEZOOM-6802b), so the
 * sampler takes a key only while one of its edges is awake (pointer in the
 * edge zone or a tab popped): digits 1 to 9 and 0 fire that edge's tabs 1 to
 * 10, PageUp / PageDown page its set. `[` and `]` are never sampler keys. A
 * key an earlier handler already claimed (preventDefault), a modifier chord or
 * a focused text field gives no action. Keys reach the store as performance
 * bus commands (samplerKeyDown, samplerKeyUp), the ones agents send: a fire key's
 * keyup sends sampler_release for the slot its press fired, which stops a gate
 * slot (#12169). Pure: no store import, the caller passes the awake edge and the
 * slot lookup in.
 */

import type { SamplerCommand } from './sampler-ipc-guards';

export type SamplerEdge = 'L' | 'R';

export type SamplerKeyAction =
	| { kind: 'fire'; edge: SamplerEdge; index: number }
	| { kind: 'flip'; edge: SamplerEdge; delta: 1 | -1 };

export interface SamplerKeyEvent {
	key: string;
	metaKey: boolean;
	ctrlKey: boolean;
	altKey: boolean;
	defaultPrevented: boolean;
	/** true for the browser's auto-repeat keydowns while a key is held */
	repeat?: boolean;
}

export interface SamplerKeyContext {
	inTextField: boolean;
	/** The edge that is awake now, or null when both edges rest. */
	awakeEdge: SamplerEdge | null;
}

export const SAMPLER_FIRE_KEYS = '1234567890';
const FLIP_KEYS: Readonly<Record<string, 1 | -1>> = { PageDown: 1, PageUp: -1 };

/** Every plain key the sampler binds while an edge is awake. */
export const SAMPLER_KEYS: readonly string[] = [...SAMPLER_FIRE_KEYS, ...Object.keys(FLIP_KEYS)];

/** The awake edge for key routing: the last-used edge when it is awake, else any awake edge, else null. */
export function awakeSamplerEdge(
	awake: Readonly<Record<SamplerEdge, boolean>>,
	lastEdge: SamplerEdge
): SamplerEdge | null {
	if (awake[lastEdge]) return lastEdge;
	const other: SamplerEdge = lastEdge === 'L' ? 'R' : 'L';
	return awake[other] ? other : null;
}

/** The sampler action a keydown asks for, or null to leave the key to the page's own bindings. */
export function samplerKeyAction(
	ev: SamplerKeyEvent,
	ctx: SamplerKeyContext
): SamplerKeyAction | null {
	const edge = ctx.awakeEdge;
	if (edge === null || ev.defaultPrevented || ctx.inTextField) return null;
	if (ev.metaKey || ev.ctrlKey || ev.altKey) return null;
	const delta = FLIP_KEYS[ev.key];
	if (delta !== undefined) return { kind: 'flip', edge, delta };
	const index = ev.key.length === 1 ? SAMPLER_FIRE_KEYS.indexOf(ev.key) : -1;
	return index >= 0 ? { kind: 'fire', edge, index } : null;
}

/** Fire keys held down now, each mapped to the slot id its press fired. */
export type SamplerKeyHolds = Map<string, string>;

/** Whether the sampler claims a keydown, and the bus command it sends (none for a repeat or an empty tab). */
export function samplerKeyDown(
	ev: SamplerKeyEvent,
	ctx: SamplerKeyContext,
	slotIdAt: (edge: SamplerEdge, index: number) => string | null,
	holds: SamplerKeyHolds
): { claimed: boolean; command: SamplerCommand | null } {
	if (ev.repeat === true && holds.has(ev.key)) return { claimed: true, command: null };
	const action = samplerKeyAction(ev, ctx);
	if (action === null) return { claimed: false, command: null };
	if (action.kind === 'flip') {
		return {
			claimed: true,
			command: { type: 'sampler_set_page', edge: action.edge, delta: action.delta }
		};
	}
	const slotId = slotIdAt(action.edge, action.index);
	if (slotId === null) return { claimed: true, command: null };
	holds.set(ev.key, slotId);
	return { claimed: true, command: { type: 'sampler_trigger', slot_id: slotId } };
}

/** The bus command a keyup sends: sampler_release for the slot its key's press fired, else null. */
export function samplerKeyUp(key: string, holds: SamplerKeyHolds): SamplerCommand | null {
	const slotId = holds.get(key);
	if (slotId === undefined) return null;
	holds.delete(key);
	return { type: 'sampler_release', slot_id: slotId };
}
