/** The verified Sets route used by the live performance REC rail. */

import {
	startRecorder,
	stopRecorder,
	type RecorderSourceName,
	type RecorderStatus
} from '../../routes/sets/sets-api';

export const PERFORMANCE_RECORDER_SOURCES: RecorderSourceName[] = [
	'djay_monitor',
	'opendj_decks'
];

function assertDeviceIndex(deviceIndex: number): void {
	if (!Number.isInteger(deviceIndex) || deviceIndex < 0) {
		throw new Error('ffmpeg device index must be a non-negative integer');
	}
}

export async function startPerformanceRecorder(deviceIndex: number): Promise<RecorderStatus> {
	assertDeviceIndex(deviceIndex);
	return startRecorder({
		session_id: null,
		ffmpeg_device_idx: deviceIndex,
		sources: PERFORMANCE_RECORDER_SOURCES
	});
}

export async function stopPerformanceRecorder(status: RecorderStatus): Promise<RecorderStatus> {
	if (!status.active || status.session_id === null) {
		throw new Error('no active recording to stop');
	}
	if (!status.owned) {
		throw new Error(`recording ${status.session_id} is owned by process ${status.pid}`);
	}
	return stopRecorder(status.session_id);
}
