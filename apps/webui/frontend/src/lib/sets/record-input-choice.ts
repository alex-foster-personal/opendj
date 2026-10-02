/** The REC input choice, the daemon's memory of it and the start call (SET-10).
 *  Kept apart from performance-recorder.ts so only the lazily loaded picker
 *  pulls it in, outside the /performance bundle budget. */

import { api, unwrap } from '$lib/api/client';
import {
	rethrowSetsError,
	startRecorder,
	type RecorderDevices,
	type RecorderSourceName,
	type RecorderStatus
} from '../../routes/sets/sets-api';

/** What REC records: one audio input by its name, or no audio at all (the
 *  set's tracklist only). By name, never index: ffmpeg renumbers inputs
 *  whenever one is plugged in, so a remembered index drifts (SET-10). */
export type RecordInputChoice =
	| { kind: 'device'; name: string }
	| { kind: 'none' };

/** The input REC last started on, kept by the daemon under its sets root, or
 *  null when unknown. Not localStorage: the desktop shell serves this UI from
 *  a loopback port the OS assigns per launch, and web storage is per port, so
 *  it would forget the choice on every restart (SET-10). The daemon writes it
 *  when a start succeeds, so the picker never saves it itself. */
export async function getRememberedInput(): Promise<RecordInputChoice | null> {
	try {
		const { remembered } = await unwrap(api.GET('/api/sets/recorder/remembered-input', {}));
		if (remembered == null) return null;
		return remembered.kind === 'device' && remembered.name
			? { kind: 'device', name: remembered.name }
			: { kind: 'none' };
	} catch (error) {
		rethrowSetsError(error);
	}
}

/** The choice the picker opens on: the remembered input while it is still
 *  connected, else the daemon's loopback default, else nothing (the DJ picks;
 *  a microphone is never chosen for them). */
export function initialInputChoice(
	remembered: RecordInputChoice | null,
	devices: RecorderDevices | null
): RecordInputChoice | null {
	if (remembered?.kind === 'none') return remembered;
	if (devices === null) return null;
	if (
		remembered?.kind === 'device' &&
		devices.devices.some((d) => d.name === remembered.name)
	) {
		return remembered;
	}
	return devices.default_name === null
		? null
		: { kind: 'device', name: devices.default_name };
}

/** Audio inputs by name for the REC picker. Rejects with the daemon's reason
 *  (no ffmpeg, not macOS) rather than resolving to an empty list. */
export async function listRecorderDevices(): Promise<RecorderDevices> {
	try {
		return await unwrap(api.GET('/api/sets/recorder/devices', {}));
	} catch (error) {
		rethrowSetsError(error);
	}
}

export const PERFORMANCE_RECORDER_SOURCES: RecorderSourceName[] = [
	'djay_monitor',
	'opendj_decks'
];

/** A blank name is refused by the daemon (422), so it is not re-checked here. */
export async function startPerformanceRecorder(
	choice: RecordInputChoice
): Promise<RecorderStatus> {
	const device = choice.kind === 'device';
	return startRecorder({
		session_id: null,
		...(device ? { device_name: choice.name } : {}),
		capture_audio: device,
		sources: PERFORMANCE_RECORDER_SOURCES
	});
}
