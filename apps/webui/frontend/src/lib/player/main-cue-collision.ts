/**
 * CUEOUT-25: MAIN must never play into the HEADPHONE CUE device.
 *
 * In `two_outputs` mode a MAIN equal to the CUE device sends the whole room
 * mix into the headphones while every liveness probe reads healthy (silver,
 * Mon 5 Oct 2026). `split_cable` and `practice` share one device by design,
 * so they never collide. Pure policy here; `headphones.ts` and the TopBar
 * banner are the shell around it.
 */
import type { HeadphoneOutputMode, HeadphoneState } from '$lib/rb/mixer-types';

/** `fixDeviceId` is computed by the caller (`preferredMasterOutputDeviceId`),
 * which keeps this module free of an import cycle with `headphones.ts`. */
export type MainOutputFault =
	| { kind: 'same_as_cue'; deviceId: string; label: string; fixDeviceId: string | null }
	| { kind: 'lost'; label: string; fixDeviceId: string | null };

type FaultInputs = Pick<
	HeadphoneState,
	'output_mode' | 'selected_output_device_id' | 'selected_master_output_device_id' | 'outputs' | 'routes'
>;

function _labelOf(outputs: FaultInputs['outputs'], deviceId: string): string {
	const label = outputs.find((output) => output.id === deviceId)?.label ?? '';
	return label.trim() === '' ? deviceId : label;
}

/** Refuse a MAIN pick that equals the selected CUE in two-outputs mode. */
export function assertMainIsNotCue(
	deviceId: string,
	cueId: string | null,
	outputMode: HeadphoneOutputMode
): void {
	if (outputMode === 'two_outputs' && cueId !== null && deviceId === cueId) {
		throw new Error(
			`MAIN output cannot be the HEADPHONE CUE device (${deviceId}): the room would get nothing. Pick a room output such as the speakers.`
		);
	}
}

/** What the TopBar must shout about, or null when MAIN is a distinct, working room output. */
export function mainOutputFault(headphones: FaultInputs, fixDeviceId: string | null): MainOutputFault | null {
	if (headphones.output_mode !== 'two_outputs') return null;
	const cueId = headphones.selected_output_device_id;
	const masterId = headphones.selected_master_output_device_id;
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
	} else if (fault.kind === 'lost') {
		return `Main output to ${fault.label} failed. The room may be silent.`;
	}
	const _exhaustive: never = fault;
	throw new Error(`Unhandled main output fault: ${String(_exhaustive)}`);
}
