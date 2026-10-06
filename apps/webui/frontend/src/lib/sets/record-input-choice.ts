/** The REC source choice, the daemon's memory of it and the start call
 *  (SET-10, SET-12). Kept apart from performance-recorder.ts so only the
 *  lazily loaded picker pulls it in, outside the /performance bundle budget. */

import { api, unwrap } from '$lib/api/client';
import { rustMode } from '$lib/audio-engine/rust-mode.svelte';
import {
	rethrowSetsError,
	startRecorder,
	type RecorderDevices,
	type RecorderSourceName,
	type RecorderStatus,
	type RecorderStartInput
} from '../../routes/sets/sets-api';

/** The picker's three views, in toggle order (SET-12). MACHINE records the
 *  app's own master mix, as djay Pro, rekordbox, Serato and Traktor do; it
 *  needs no driver. LOOPBACK records a loopback input (BlackHole). MIDI is a
 *  hardware mixer or controller whose mix comes back in through an audio
 *  interface input. */
export type RecordMode = 'machine' | 'loopback' | 'midi';

export const RECORD_MODES: readonly { mode: RecordMode; label: string; title: string }[] = [
	{ mode: 'machine', label: 'MACHINE', title: "Record Open DJ's own master mix: no driver needed" },
	{ mode: 'loopback', label: 'LOOPBACK', title: 'Record a loopback input such as BlackHole' },
	{
		mode: 'midi',
		label: 'MIDI',
		title: 'A hardware mixer or controller: record the audio interface input its mix comes back on'
	}
];

// TODO(website): point at our own "record with a loopback" page once it exists.
export const INSTALL_LOOPBACK_URL = 'https://existential.audio/blackhole/';

/** Why MACHINE cannot record the master mix in this page, or null when it
 *  can (SET-12): the Rust engine preview mixes outside the page. */
export function masterMixUnavailableReason(engineIsRust: boolean = rustMode.enabled): string | null {
	return engineIsRust
		? 'The Rust engine (preview) mixes outside the page, so its master mix cannot be recorded yet. Switch Settings > Audio engine to Web Audio, or record a loopback input.'
		: null;
}

/** What REC records: the master mix, one audio input by its name, or no
 *  audio at all (the set's tracklist only). By name, never index: ffmpeg
 *  renumbers inputs whenever one is plugged in, so a remembered index drifts
 *  (SET-10). */
export type RecordInputChoice =
	| { kind: 'master' }
	| { kind: 'device'; name: string }
	| { kind: 'none' };

/** The choice REC last started on, kept by the daemon under its sets root,
 *  or null when unknown. Not localStorage: the desktop shell serves this UI
 *  from a loopback port the OS assigns per launch, and web storage is per
 *  port, so it would forget the choice on every restart (SET-10). The daemon
 *  writes it when a start succeeds, so the picker never saves it itself. */
export async function getRememberedInput(): Promise<RecordInputChoice | null> {
	try {
		const { remembered } = await unwrap(api.GET('/api/sets/recorder/remembered-input', {}));
		if (remembered == null) return null;
		if (remembered.kind === 'device' && remembered.name) {
			return { kind: 'device', name: remembered.name };
		}
		return remembered.kind === 'master' ? { kind: 'master' } : { kind: 'none' };
	} catch (error) {
		rethrowSetsError(error);
	}
}

type Device = RecorderDevices['devices'][number];

/** The inputs a view lists: loopbacks under LOOPBACK, every other input
 *  (the audio interface) under MIDI, none under MACHINE. */
export function devicesForMode(devices: RecorderDevices | null, mode: RecordMode): Device[] {
	if (devices === null || mode === 'machine') return [];
	return devices.devices.filter((d) => d.loopback === (mode === 'loopback'));
}

/** The choice a view opens on when the DJ switches to it: the master mix,
 *  the daemon's loopback default, or nothing under MIDI (the DJ picks; a
 *  microphone is never chosen for them). */
export function defaultChoiceForMode(
	mode: RecordMode,
	devices: RecorderDevices | null,
	masterAvailable: boolean
): RecordInputChoice | null {
	if (mode === 'machine') return masterAvailable ? { kind: 'master' } : null;
	if (mode === 'midi') return null;
	const loopbacks = devicesForMode(devices, 'loopback');
	const name =
		loopbacks.find((d) => d.name === devices?.default_name)?.name ?? loopbacks[0]?.name ?? null;
	return name === null ? null : { kind: 'device', name };
}

/** The view and choice the picker opens on: the remembered choice while it
 *  can still be made, else the master mix (SET-12), else the loopback default. */
export function initialRecordSelection(
	remembered: RecordInputChoice | null,
	devices: RecorderDevices | null,
	masterAvailable: boolean
): { mode: RecordMode; choice: RecordInputChoice | null } {
	if (remembered?.kind === 'master' && masterAvailable) return { mode: 'machine', choice: remembered };
	if (remembered?.kind === 'none') return { mode: 'midi', choice: remembered };
	if (remembered?.kind === 'device') {
		const device = devices?.devices.find((d) => d.name === remembered.name);
		if (device) return { mode: device.loopback ? 'loopback' : 'midi', choice: remembered };
	}
	const mode: RecordMode = masterAvailable ? 'machine' : 'loopback';
	return { mode, choice: defaultChoiceForMode(mode, devices, masterAvailable) };
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

/** The start request for a choice made in a view; the source is always
 *  explicit (SET-12). A blank name is refused by the daemon (422). */
export function startRequestFor(choice: RecordInputChoice, mode: RecordMode): RecorderStartInput {
	const base = { session_id: null, sources: PERFORMANCE_RECORDER_SOURCES };
	if (choice.kind !== 'device') return { ...base, source: choice.kind };
	return { ...base, source: mode === 'loopback' ? 'loopback' : 'external', device_name: choice.name };
}

export async function startPerformanceRecorder(
	choice: RecordInputChoice,
	mode: RecordMode
): Promise<RecorderStatus> {
	return startRecorder(startRequestFor(choice, mode));
}
