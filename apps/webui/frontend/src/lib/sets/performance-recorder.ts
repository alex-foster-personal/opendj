/** The verified Sets route used by the live performance REC rail. Starting a
 *  recording lives in record-input-choice.ts, behind the lazily loaded picker. */

import { stopRecorder, type RecorderStatus } from '../../routes/sets/sets-api';

/** How the REC button shows a recorder status (SET-11). REC lights only once
 *  audio is really being written: while macOS's first-run microphone prompt
 *  is up the input hands out silence, and a lit REC then records nothing. */
export interface RecordRailState {
	/** Light REC as recording. */
	recording: boolean;
	/** The microphone prompt is up; nothing is written yet. */
	waiting: boolean;
	/** Tooltip replacing the default, or null for the default. */
	tip: string | null;
	/** Poll the status again soon: the capture state is still moving. */
	poll: boolean;
}

export function recordRailState(status: RecorderStatus): RecordRailState {
	if (!status.active) {
		return { recording: false, waiting: false, tip: null, poll: false };
	}
	switch (status.capture) {
		case 'waiting_permission':
			return {
				recording: false,
				waiting: true,
				tip: 'Waiting for microphone permission: answer the macOS prompt to start recording (click to cancel)',
				poll: true
			};
		case 'starting':
			return { recording: false, waiting: true, tip: 'Starting the audio input (click to cancel)', poll: true };
		case 'failed':
		case 'stopped':
			return {
				recording: false,
				waiting: false,
				tip: 'The audio input stopped recording; click to stop and keep what was recorded',
				poll: false
			};
		default:
			// recording, none (tracklist only) and unknown (another process owns it).
			return { recording: true, waiting: false, tip: null, poll: false };
	}
}

export async function stopPerformanceRecorder(
	status: RecorderStatus
): Promise<RecorderStatus> {
	if (!status.active || status.session_id === null) {
		throw new Error('no active recording to stop');
	}
	if (!status.owned) {
		throw new Error(
			`recording ${status.session_id} is owned by process ${status.pid}`
		);
	}
	return stopRecorder(status.session_id);
}
