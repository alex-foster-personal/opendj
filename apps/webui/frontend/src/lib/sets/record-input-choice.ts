/** The REC input choice, its per-machine memory and the start call (SET-10).
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

export const RECORD_INPUT_STORAGE_KEY = 'opendj.setRecord.input.v1';

type ChoiceStorage = Pick<Storage, 'getItem' | 'setItem'>;

function browserStorage(): ChoiceStorage | null {
	try {
		return typeof localStorage === 'undefined' ? null : localStorage;
	} catch {
		return null;
	}
}

function isChoice(value: unknown): value is RecordInputChoice {
	if (typeof value !== 'object' || value === null) return false;
	const kind = (value as { kind?: unknown }).kind;
	if (kind === 'none') return true;
	const name = (value as { name?: unknown }).name;
	return kind === 'device' && typeof name === 'string' && name.length > 0;
}

/** The input the DJ last recorded from on this machine, or null. */
export function loadRememberedInput(
	storage: ChoiceStorage | null = browserStorage()
): RecordInputChoice | null {
	try {
		const raw = storage?.getItem(RECORD_INPUT_STORAGE_KEY);
		if (raw == null) return null;
		const parsed: unknown = JSON.parse(raw);
		return isChoice(parsed) ? parsed : null;
	} catch {
		return null;
	}
}

export function rememberInput(
	choice: RecordInputChoice,
	storage: ChoiceStorage | null = browserStorage()
): void {
	try {
		storage?.setItem(RECORD_INPUT_STORAGE_KEY, JSON.stringify(choice));
	} catch {
		// A private window or blocked storage only costs the preselection.
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
