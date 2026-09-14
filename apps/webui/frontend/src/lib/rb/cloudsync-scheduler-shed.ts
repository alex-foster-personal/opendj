/**
 * PERFMODE-04 shed job for the CloudSync metadata scheduler (CLOUDSYNC-09 part 1).
 *
 * When pressure or xruns are elevated while a deck is playing, the shed
 * controller owes a resume call. Draining posts to the operator route so the
 * engine scheduler can run the coalesced round once signals clear.
 */

import { resumeCloudsyncSchedulerOwed } from '$lib/api-cloudsync-ops';
import {
	pressureIsElevated,
	readMachinePressure,
	S1_XRUN_DELTA_MAX,
	subscribeMachinePressure
} from './machine-pressure';
import { anyDeckPlaying } from './playing-gate';
import type { BackgroundDemandShed } from './playing-gate';

let _xrunsAtPreviousArm = 0;

function _xrunWindowElevated(readXruns: () => number): boolean {
	return readXruns() - _xrunsAtPreviousArm > S1_XRUN_DELTA_MAX;
}

function _shouldRequestCloudsyncShed(
	shed: BackgroundDemandShed,
	readXruns: () => number
): boolean {
	if (!anyDeckPlaying()) return false;
	if (!pressureIsElevated(readMachinePressure()) && !_xrunWindowElevated(readXruns)) {
		return false;
	}
	shed.request('cloudsync-scheduler');
	return true;
}

/** Subscribe pressure ticks to request cloudsync-scheduler deferral while elevated. */
export function armCloudsyncSchedulerShed(
	shed: BackgroundDemandShed,
	readXruns: () => number
): () => void {
	_xrunsAtPreviousArm = readXruns();
	return subscribeMachinePressure(() => {
		_shouldRequestCloudsyncShed(shed, readXruns);
		_xrunsAtPreviousArm = readXruns();
	});
}

/** Drain callback: wake the owed scheduler round via the operator route. */
export async function resumeCloudsyncSchedulerOwedJob(): Promise<void> {
	await resumeCloudsyncSchedulerOwed();
}
