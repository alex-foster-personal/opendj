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
	/** Read the status again after this many ms, or null not to: fast while
	 * the capture is starting or waiting, slower while it records, so a
	 * capture that fails mid-set (input unplugged) still unlights REC. */
	poll: number | null;
}

export function recordRailState(status: RecorderStatus): RecordRailState {
	if (!status.active) {
		return { recording: false, waiting: false, tip: null, poll: null };
	}
	switch (status.capture) {
		case 'waiting_permission':
			return {
				recording: false,
				waiting: true,
				tip: 'Waiting for microphone permission: answer the macOS prompt to start recording (click to cancel)',
				poll: 1000
			};
		case 'starting':
			return { recording: false, waiting: true, tip: 'Starting the audio input (click to cancel)', poll: 1000 };
		case 'failed':
		case 'stopped':
			return {
				recording: false,
				waiting: false,
				tip: 'The audio input stopped recording; click to stop and keep what was recorded',
				poll: null
			};
		case 'recording':
			return { recording: true, waiting: false, tip: null, poll: 3000 };
		default:
			// none (tracklist only) and unknown (another process owns it): no
			// capture of ours whose state can change.
			return { recording: true, waiting: false, tip: null, poll: null };
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
