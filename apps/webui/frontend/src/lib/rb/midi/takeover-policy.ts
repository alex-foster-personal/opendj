/**
 * IOPIN-06 absolute-control pickup policy.
 *
 * This is deliberately a production-pure state machine: WebMIDI owns wire
 * decoding and action-glue owns engine dispatch, while this module decides
 * whether an *absolute* value is safe to apply. It never synthesizes a
 * hardware value. Relative encoders never enter this policy.
 */

import type { MidiAction } from './midi-types';

export type MidiTakeoverMode = 'pickup' | 'jump';

export interface TakeoverIdentity {
	deviceId: string;
	/** Physical MIDI source, including its layer-specific binding. */
	controlId: string;
	/** The scalar the action controls (deck + function included). */
	functionId: string;
}

export interface TakeoverGhost {
	/** Last value actually observed from hardware; never a guessed position. */
	value: number;
	/** Current engine/UI scalar that must be reached before pickup. */
	target: number;
}

export interface TakeoverDecision {
	apply: boolean;
	ghost: TakeoverGhost | null;
}

interface TakeoverState extends TakeoverIdentity {
	target: number;
	lastHardwareValue: number;
	armed: boolean;
}

const VALUE_EPSILON = 1e-9;

/** Stable scalar key shared by the engine sync and reusable control chrome. */
export function continuousTakeoverFunction(action: MidiAction): string | null {
	switch (action.type) {
		case 'mixer_channel':
			return action.target === 'eq'
				? action.band === undefined
					? null
					: `mixer:${action.deck}:eq:${action.band}`
				: `mixer:${action.deck}:${action.target}`;
		case 'mixer_global':
			return `mixer:global:${action.target}`;
		case 'headphone_mix':
			return 'headphones:mix';
		case 'headphone_level':
			return 'headphones:level';
		case 'deck_pitch':
			return `deck:${action.deck}:pitch`;
		default:
			return null;
	}
}

/** The full identity is intentionally not just a function: two controllers,
 * or two layered controls on one controller, must pick up independently. */
export function makeTakeoverIdentity(
	deviceId: string,
	controlId: string,
	action: MidiAction
): TakeoverIdentity | null {
	const functionId = continuousTakeoverFunction(action);
	return functionId === null ? null : { deviceId, controlId, functionId };
}

function identityKey(identity: TakeoverIdentity): string {
	return `${identity.deviceId}\u0000${identity.controlId}\u0000${identity.functionId}`;
}

function assertUnitInterval(name: string, value: number): void {
	if (!Number.isFinite(value) || value < 0 || value > 1) {
		throw new RangeError(`${name} must be a finite value in 0..1, got ${value}`);
	}
}

function crosses(previous: number, next: number, target: number): boolean {
	return (previous <= target && target <= next) || (next <= target && target <= previous);
}

/**
 * Per-device/control scalar ownership. `observeAbsolute` records the real
 * input first, then permits it only at pickup. A caller that receives
 * `apply: true` must call `noteHardwareApplied` before issuing its engine
 * command; that prevents the subsequent engine read-model update being
 * mistaken for an independent software edit.
 */
export class AbsoluteTakeoverPolicy {
	#mode: MidiTakeoverMode;
	#states = new Map<string, TakeoverState>();

	constructor(mode: MidiTakeoverMode = 'pickup') {
		this.#mode = mode;
	}

	get mode(): MidiTakeoverMode {
		return this.#mode;
	}

	setMode(mode: MidiTakeoverMode): void {
		this.#mode = mode;
	}

	observeAbsolute({
		identity,
		hardwareValue,
		softwareValue,
		step = 1 / 127
	}: {
		identity: TakeoverIdentity;
		hardwareValue: number;
		softwareValue: number;
		/** One physical MIDI value increment: 1/127 or 1/16383. */
		step?: number;
	}): TakeoverDecision {
		assertUnitInterval('hardwareValue', hardwareValue);
		assertUnitInterval('softwareValue', softwareValue);
		if (!Number.isFinite(step) || step <= 0 || step > 1) {
			throw new RangeError(`step must be a finite value in (0, 1], got ${step}`);
		}

		const key = identityKey(identity);
		let state = this.#states.get(key);
		if (state === undefined) {
			state = { ...identity, target: softwareValue, lastHardwareValue: hardwareValue, armed: true };
			this.#states.set(key, state);
		} else {
			if (Math.abs(state.target - softwareValue) > VALUE_EPSILON) {
				state.target = softwareValue;
				state.armed = true;
			}
			const previous = state.lastHardwareValue;
			state.lastHardwareValue = hardwareValue;
			if (this.#mode === 'jump') {
				state.target = hardwareValue;
				state.armed = false;
				return { apply: true, ghost: null };
			}
			if (!state.armed) return { apply: true, ghost: null };
			if (Math.abs(hardwareValue - state.target) <= step + VALUE_EPSILON || crosses(previous, hardwareValue, state.target)) {
				state.target = hardwareValue;
				state.armed = false;
				return { apply: true, ghost: null };
			}
			return { apply: false, ghost: { value: hardwareValue, target: state.target } };
		}

		if (this.#mode === 'jump') {
			state.target = hardwareValue;
			state.armed = false;
			return { apply: true, ghost: null };
		}
		if (Math.abs(hardwareValue - state.target) <= step + VALUE_EPSILON) {
			state.target = hardwareValue;
			state.armed = false;
			return { apply: true, ghost: null };
		}
		return { apply: false, ghost: { value: hardwareValue, target: state.target } };
	}

	/** Re-arm only states that have actually seen that scalar from hardware. */
	noteSoftwareValue(functionId: string, value: number): void {
		assertUnitInterval('softwareValue', value);
		for (const state of this.#states.values()) {
			if (state.functionId !== functionId || Math.abs(state.target - value) <= VALUE_EPSILON) continue;
			state.target = value;
			state.armed = true;
		}
	}

	/** Register a successful engine command as the action just authorized. */
	noteHardwareApplied(identity: TakeoverIdentity, value: number): void {
		assertUnitInterval('hardwareValue', value);
		const state = this.#states.get(identityKey(identity));
		if (state === undefined) return;
		state.target = value;
		state.lastHardwareValue = value;
		state.armed = false;
	}

	/** Disconnect and layer transitions require a fresh physical pickup. */
	rearmDevice(deviceId: string): void {
		for (const state of this.#states.values()) {
			if (state.deviceId === deviceId) state.armed = true;
		}
	}

	ghostForFunction(functionId: string): TakeoverGhost | null {
		if (this.#mode === 'jump') return null;
		let latest: TakeoverState | null = null;
		for (const state of this.#states.values()) {
			if (state.functionId === functionId && state.armed) latest = state;
		}
		return latest === null ? null : { value: latest.lastHardwareValue, target: latest.target };
	}
}
