/** The verified Sets route used by the live performance REC rail. Starting a
 *  recording lives in record-input-choice.ts, behind the lazily loaded picker. */

import { stopRecorder, type RecorderStatus } from '../../routes/sets/sets-api';

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
