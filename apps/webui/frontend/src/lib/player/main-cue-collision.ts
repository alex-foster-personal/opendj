/**
 * CUEOUT-25 / CUEOUT-26: MAIN must never play into the HEADPHONE CUE device,
 * and never onto a device that cannot sound.
 *
 * In `two_outputs` mode a MAIN equal to the CUE device sends the whole room
 * mix into the headphones while every liveness probe reads healthy (silver,
 * Mon 5 Oct 2026). CUEOUT-25 re-picked MAIN to a "speaker" by label, which on
 * a MacBook with wired headphones is the built-in speaker the jack has muted
 * (silver, Tue 6 Oct 2026, build 10: silence). CUEOUT-26: when MAIN and CUE
 * are one PHYSICAL output -- the same id, or the MacBook speaker/jack pair
 * while the jack is occupied -- the cue runs as split cue on that output
 * (master L / cue R, `split_cable`), and a device that cannot sound is never
 * a MAIN. Pure policy here; `headphones.ts` and the TopBar are the shell.
 */
import type { HeadphoneState } from '$lib/rb/mixer-types';

/** `fixDeviceId` is computed by the caller (`preferredMasterOutputDeviceId`),
 * which keeps this module free of an import cycle with `headphones.ts`. */
export type MainOutputFault =
	| { kind: 'same_as_cue'; deviceId: string; label: string; fixDeviceId: string | null }
	| { kind: 'cannot_sound'; deviceId: string; label: string; fixDeviceId: string | null }
	| { kind: 'lost'; label: string; fixDeviceId: string | null };

type FaultInputs = Pick<
	HeadphoneState,
	'output_mode' | 'selected_output_device_id' | 'selected_master_output_device_id' | 'outputs' | 'routes'
>;

function _labelOf(outputs: FaultInputs['outputs'], deviceId: string): string {
	const label = outputs.find((output) => output.id === deviceId)?.label ?? '';
	return label.trim() === '' ? deviceId : label;
}

type OutputRow = FaultInputs['outputs'][number];

/** The id of the output that actually sounds for `deviceId`. The Mac shell
 * names it (`physical_id`: the occupied jack for the muted built-in
 * speakers); a browser listing has no such field, so every id is its own. */
export function physicalOutputId(outputs: readonly OutputRow[], deviceId: string): string {
	return outputs.find((output) => output.id === deviceId)?.physical_id ?? deviceId;
}

/** True when two ids are ONE physical output, so MAIN and CUE cannot be two outputs on them. */
export function sameOutputDevice(outputs: readonly OutputRow[], a: string, b: string): boolean {
	return physicalOutputId(outputs, a) === physicalOutputId(outputs, b);
}

/** True for an output the shell knows is silent right now (the jack-muted speakers). */
export function outputCannotSound(outputs: readonly OutputRow[], deviceId: string): boolean {
	return outputs.find((output) => output.id === deviceId)?.muted_by_jack === true;
}

/** Refuse a MAIN that cannot sound, before the live MAIN route is touched. */
export function assertMasterCanSound(deviceId: string, outputs: readonly OutputRow[]): void {
	if (outputCannotSound(outputs, deviceId)) {
		throw new Error(
			`MAIN output cannot be ${_labelOf(outputs, deviceId)}: the MacBook speakers are muted while headphones are in the headphone jack, so the room would get nothing. Choose the headphones (split cue) or another output.`
		);
	}
}

/** What the TopBar must shout about, or null when MAIN is a distinct, working room output. */
export function mainOutputFault(headphones: FaultInputs, fixDeviceId: string | null): MainOutputFault | null {
	const masterId = headphones.selected_master_output_device_id;
	if (masterId !== null && outputCannotSound(headphones.outputs, masterId)) {
		return { kind: 'cannot_sound', deviceId: masterId, label: _labelOf(headphones.outputs, masterId), fixDeviceId };
	}
	if (headphones.output_mode !== 'two_outputs') return null;
	const cueId = headphones.selected_output_device_id;
	if (cueId !== null && masterId === cueId) {
		return { kind: 'same_as_cue', deviceId: masterId, label: _labelOf(headphones.outputs, masterId), fixDeviceId };
	} else if (headphones.routes.master.state === 'failed') {
		const label = masterId === null ? 'the system default' : _labelOf(headphones.outputs, masterId);
		return { kind: 'lost', label, fixDeviceId };
	}
	return null;
}

/** The MAIN a refresh moved off the CUE device, so the shell persists and announces it. */
export function masterRepairedFromCue(args: {
	previousMasterId: string | null;
	cueId: string | null;
	nextMasterId: string | null;
	autoPinnedMaster: boolean;
}): { from: string; to: string } | null {
	if (
		args.autoPinnedMaster &&
		args.previousMasterId !== null &&
		args.nextMasterId !== null &&
		args.previousMasterId === args.cueId &&
		args.nextMasterId !== args.cueId
	) {
		return { from: args.previousMasterId, to: args.nextMasterId };
	}
	return null;
}

export function mainOutputFaultText(fault: MainOutputFault): string {
	if (fault.kind === 'same_as_cue') {
		return `Main output is going to ${fault.label} (same as headphones). The room gets nothing.`;
	} else if (fault.kind === 'cannot_sound') {
		return `Main output is set to ${fault.label}, which the headphone jack has muted. The room gets nothing.`;
	} else if (fault.kind === 'lost') {
		return `Main output to ${fault.label} failed. The room may be silent.`;
	}
	const _exhaustive: never = fault;
	throw new Error(`Unhandled main output fault: ${String(_exhaustive)}`);
}

/** The visible line while CUEOUT-26 runs the cue as split cue on one output. */
export function sameDeviceSplitText(label: string): string {
	return `Split cue: master L / cue R (same device: ${label})`;
}
