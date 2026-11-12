/**
 * Practice replica read side: what each on-screen control SHOWS, from the state the engine
 * already publishes (issue #8884, spec (private path)). The input side
 * is untouched (ADR-0137): this module never dispatches.
 *
 * Requirements (mini-PRD):
 *   ✔︎ A bound knob, fader or crossfader shows the engine's scalar for its action (the same
 *     `softwareScalar` the takeover layer compares hardware against), never a fixed 0.5.
 *     [if] CH 1 FADER at engine 0.8 renders at anything but 0.8 [then] ⛔️
 *   ✔︎ A bound continuous control whose action has no engine scalar is 'no-source', rendered
 *     inert with the PARITY-TODO title (house rule: no invented value).
 *     [if] a no-scalar control renders a range input with any value [then] ⛔️
 *   ✔︎ When the takeover layer holds a ghost for the control's function (hardware last reported a
 *     position the engine has not picked up), the control carries that hardware value too.
 *     [if] HI 1 with ghost 0.85 and engine 0.5 shows no ghost marker [then] ⛔️
 *   ✔︎ A button or pad with an LED rule in the device map is lit exactly when the engine's LED
 *     snapshot (midiLedFeedback, the one the hardware gets) carries that rule's on velocity.
 *     A button with no LED rule never shows lit.
 *     [if] deck 1 PLAY shows lit while the snapshot says off [then] ⛔️
 *     [if] deck 1 BEAT SYNC shows lit while the snapshot says off [then] ⛔️
 */

import type { DeviceMap, MidiAction } from '$lib/rb/midi/midi-types';
import { continuousTakeoverFunction, type TakeoverGhost } from '$lib/rb/midi/takeover-policy';
import type { ResolvedControl } from './practice-bindings';

export interface LedOut {
	ch: number;
	note: number;
	velocity: number;
}

/** Engine read seams. The replica passes the live stores; tests pass recorded values. */
export interface LiveReaders {
	scalar: (action: MidiAction) => number | null;
	ghost: (functionId: string) => TakeoverGhost | null;
	leds: readonly LedOut[];
}

export type ControlLive =
	| { kind: 'value'; value: number; ghost: number | null }
	| { kind: 'no-source' }
	| { kind: 'lit'; on: boolean }
	| { kind: 'none' };

export const NO_SOURCE_TITLE = 'not implemented - see PARITY-TODO (no engine value to show)';

const CONTINUOUS = new Set(['fader', 'knob', 'crossfader']);

export function isContinuousControl(r: ResolvedControl): boolean {
	return CONTINUOUS.has(r.control.type);
}

const _clamp01 = (v: number): number => Math.min(1, Math.max(0, v));

/** What one resolved control shows right now. Inert controls show nothing live. */
export function controlLive(r: ResolvedControl, map: DeviceMap, read: LiveReaders): ControlLive {
	if (r.kind !== 'bound') return { kind: 'none' };
	if (isContinuousControl(r)) {
		const value = read.scalar(r.binding.action);
		if (value === null || !Number.isFinite(value)) return { kind: 'no-source' };
		const fid = continuousTakeoverFunction(r.binding.action);
		const ghost = fid === null ? null : read.ghost(fid);
		return {
			kind: 'value',
			value: _clamp01(value),
			ghost: ghost === null ? null : _clamp01(ghost.value)
		};
	}
	if (r.wire.kind !== 'note') return { kind: 'none' };
	const rules = (map.leds ?? []).filter((l) => l.out.ch === r.wire.ch && l.out.note === r.wire.id);
	if (rules.length === 0) return { kind: 'none' };
	const out = read.leds.find((o) => o.ch === r.wire.ch && o.note === r.wire.id);
	// midiLedFeedback emits one entry per addressed LED; an address with rules but no entry is a bug.
	if (out === undefined) {
		throw new Error(
			`LED snapshot has no entry for ${r.control.id} (ch ${r.wire.ch} note ${r.wire.id})`
		);
	}
	return {
		kind: 'lit',
		on: rules.some(
			(l) => l.out.velocityOn === out.velocity && l.out.velocityOn !== l.out.velocityOff
		)
	};
}

/** Every control's live state, keyed by control id. One pass per engine change. */
export function liveByControl(
	resolved: readonly ResolvedControl[],
	map: DeviceMap,
	read: LiveReaders
): Map<string, ControlLive> {
	return new Map(resolved.map((r) => [r.control.id, controlLive(r, map, read)]));
}
