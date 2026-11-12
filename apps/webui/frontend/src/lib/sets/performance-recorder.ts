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
	/** True when this surface may call stopPerformanceRecorder (owned active session). */
	stoppable: boolean;
}

/** How often an idle REC re-reads the recorder (SET-12). */
export const IDLE_POLL_MS = 5000;

/** `m:ss`, or `h:mm:ss` from one hour (754 s gives `12:34`). */
export function formatRecordingElapsed(seconds: number): string {
	const total = Math.floor(seconds);
	const h = Math.floor(total / 3600);
	const m = Math.floor((total % 3600) / 60);
	const s = String(total % 60).padStart(2, '0');
	return h > 0 ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`;
}

/** Decimal SI size: `1.2 GB` from 1e9 bytes, otherwise whole `MB`. */
export function formatRecordingBytes(bytes: number): string {
	return bytes >= 1e9 ? `${(bytes / 1e9).toFixed(1)} GB` : `${Math.round(bytes / 1e6)} MB`;
}

/** `clipped N times` when the master mix clipped (issue #10371), else empty. */
export function clipSuffix(clipEvents: number | null | undefined): string {
	if (!clipEvents) return '';
	return clipEvents === 1 ? 'clipped 1 time' : `clipped ${clipEvents} times`;
}

/** `Recording 12:34, 1.2 GB, clipped 3 times`; null without an elapsed time. */
export function recordingTip(status: RecorderStatus): string | null {
	if (status.elapsed_s == null) return null;
	const parts = [`Recording ${formatRecordingElapsed(status.elapsed_s)}`];
	if (status.audio_bytes != null) parts.push(formatRecordingBytes(status.audio_bytes));
	const clipped = clipSuffix(status.clip_events);
	if (clipped) parts.push(clipped);
	return parts.join(', ');
}

/** Session-scoped capture failure the daemon may stop sending on later polls. */
export interface RetainedCaptureFailure {
	sessionId: string;
	reason: string | null;
}

const STOPPED_CLEAN_TIP =
	'The audio input stopped recording; click to stop and keep what was recorded';

function failureRailTip(status: RecorderStatus, retained: RetainedCaptureFailure | null): string {
	const retainedReason = retained?.sessionId === status.session_id ? retained.reason : null;
	const why = status.capture_error ?? retainedReason;
	if (why) {
		return `Set recording failed: ${why}. Press REC to stop and keep what was recorded.`;
	}
	return 'Set recording failed: the audio input stopped. Press REC to stop and keep what was recorded.';
}

/** Merge session-scoped failure retention across recorder status refreshes. */
export function updateRetainedCaptureFailure(
	prev: RetainedCaptureFailure | null,
	status: RecorderStatus
): RetainedCaptureFailure | null {
	if (!status.active) return null;
	const sessionId = status.session_id;
	if (sessionId === null) return null;
	if (status.capture === 'failed') {
		return { sessionId, reason: status.capture_error ?? null };
	}
	if (prev !== null && prev.sessionId === sessionId) return prev;
	return null;
}

export function recordRailState(
	status: RecorderStatus,
	retainedFailure: RetainedCaptureFailure | null = null
): RecordRailState {
	if (!status.active) {
		// SET-12: a recording an agent starts over HTTP (`python -m apps.sets rec
		// start`) is seen within this, so a master recording gets its tap.
		return { recording: false, waiting: false, tip: null, poll: IDLE_POLL_MS, stoppable: false };
	}
	if (!status.owned) {
		const tip = status.recoverable
			? `Recording owner exited (process ${status.pid}); finalize on Sessions / REC`
			: `Owned by process ${status.pid}; stop from that process or Sessions / REC`;
		return { recording: true, waiting: false, tip, poll: null, stoppable: false };
	}
	switch (status.capture) {
		case 'waiting_permission':
			return {
				recording: false,
				waiting: true,
				tip: 'Waiting for microphone permission: answer the macOS prompt to start recording (click to cancel)',
				poll: 1000,
				stoppable: false
			};
		case 'starting':
			return {
				recording: false,
				waiting: true,
				tip: 'Starting the audio input (click to cancel)',
				poll: 1000,
				stoppable: false
			};
		case 'failed':
			return {
				recording: false,
				waiting: false,
				tip: failureRailTip(status, retainedFailure),
				poll: null,
				stoppable: true
			};
		case 'stopped': {
			const retainedForSession =
				retainedFailure?.sessionId === status.session_id ? retainedFailure : null;
			return {
				recording: false,
				waiting: false,
				tip:
					retainedForSession !== null ? failureRailTip(status, retainedFailure) : STOPPED_CLEAN_TIP,
				poll: null,
				stoppable: true
			};
		}
		case 'recording':
			return {
				recording: true,
				waiting: false,
				tip: recordingTip(status),
				poll: 3000,
				stoppable: true
			};
		default:
			// none (tracklist only): no capture of ours whose state can change.
			return { recording: true, waiting: false, tip: null, poll: null, stoppable: true };
	}
}

/** The toast for a capture that ended on its own: the engine's reason when
 *  it gave one (a microphone denied at a late prompt names System Settings),
 *  else a generic line. Null while the capture has not failed. */
export function captureFailureMessage(status: RecorderStatus): string | null {
	if (!status.active || (status.capture !== 'failed' && status.capture !== 'stopped')) return null;
	const why = status.capture_error ? `: ${status.capture_error}` : '';
	return `Set recording: the audio input stopped${why}. Press REC to stop and keep what was recorded.`;
}

/** The recorder's headline for the /sets panel, from the same states as REC. */
export function recorderHeadline(status: RecorderStatus): string {
	if (!status.active) return 'Recorder ready';
	const rail = recordRailState(status);
	if (rail.recording) return 'Recording';
	if (status.capture === 'waiting_permission') return 'Waiting for microphone permission';
	if (rail.waiting) return 'Starting the audio input';
	if (status.capture === 'failed') return 'Audio input failed';
	return 'Audio input stopped';
}

export async function stopPerformanceRecorder(status: RecorderStatus): Promise<RecorderStatus> {
	if (!status.active || status.session_id === null) {
		throw new Error('no active recording to stop');
	}
	if (!status.owned) {
		if (status.recoverable) {
			throw new Error(`recording owner exited (process ${status.pid}); finalize on Sessions / REC`);
		}
		throw new Error(`recording ${status.session_id} is owned by process ${status.pid}`);
	}
	return stopRecorder(status.session_id);
}

/** Pin 894af5672c3b recording half: informational hover uses instant; the small
 *  REC panel is interactive and stays while the pointer is over it. */
export const RECORD_CONTROL_EXPLAINER_DISMISS = 'stay' as const;

/** [if] recording is idle [then] a REC click opens the source panel only. */
export function recordIdleClickOpensPicker(active: boolean): boolean {
	return !active;
}

const RECORD_EXPLAINER_BULLETS = [
	'Open this panel to pick MACHINE, LOOPBACK, or MIDI, then press Start recording.',
	'While a recording runs, REC lights only once audio is actually being written.',
	'Keep /performance open for a master-mix recording.'
] as const;

/** Hover copy for the Prep|Gig REC control (top bar). */
export function recordControlExplainerBullets(): readonly string[] {
	return RECORD_EXPLAINER_BULLETS;
}

/** Default tooltip when the recorder is idle. */
export function recordControlIdleTitle(): string {
	return 'Set recording - click to configure and start';
}

export interface RecordingFileRow {
	session_id: string;
	file_name: string;
	size_bytes: number;
}

/** Flatten session audio segments into rows for the library Recordings tab. */
export function recordingRowsFromSegments(
	sessions: readonly { session_id: string }[],
	segmentsBySession: ReadonlyMap<string, readonly { name: string; size_bytes: number }[]>
): RecordingFileRow[] {
	const rows: RecordingFileRow[] = [];
	for (const session of sessions) {
		const segments = segmentsBySession.get(session.session_id) ?? [];
		for (const segment of segments) {
			rows.push({
				session_id: session.session_id,
				file_name: segment.name,
				size_bytes: segment.size_bytes
			});
		}
	}
	return rows.sort((a, b) => b.file_name.localeCompare(a.file_name));
}
