/**
 * IOPIN-14: what the audio I/O panel may claim about the machine's devices.
 *
 * `navigator.mediaDevices.enumerateDevices()` answers three different
 * questions with look-alike values, and the panel used to draw all three as
 * the same empty menu:
 *
 *   1. "these are the devices"            - real ids and labels
 *   2. "I will not tell you yet"          - one placeholder per kind with an
 *                                           EMPTY deviceId and label, which is
 *                                           what Chromium returns until the
 *                                           page holds a media permission
 *   3. "I could not ask"                  - no API, a rejection, a timeout
 *
 * This module is the pure half: it turns a raw enumeration (or a failure)
 * into a list that always carries the system default output, plus a NAMED
 * access state the panel renders with an action. Nothing here touches the
 * browser, so every branch is unit-testable with a plain array.
 */

import type {
	HeadphoneOutputDevice,
	IoDeviceAccess,
	IoDeviceAccessAction,
	IoDeviceAccessStatus
} from '$lib/rb/mixer-types';

/** Chromium's own id for "whatever the OS default output is". It is accepted
 * by `setSinkId` with or without a media permission, so the synthesized entry
 * below is selectable, not decoration. */
export const SYSTEM_DEFAULT_OUTPUT_ID = 'default';
export const SYSTEM_DEFAULT_OUTPUT_LABEL = 'System default output';

/** `withHeadphoneOperationTimeout` stamps this name so a timeout is told from
 * a rejection by type, never by matching message text. */
export const IO_OPERATION_TIMEOUT_ERROR_NAME = 'HeadphoneOperationTimeoutError';

type EnumeratedDevice = Pick<MediaDeviceInfo, 'kind' | 'deviceId' | 'label'> & Partial<Pick<MediaDeviceInfo, 'groupId'>>;

export interface IoDeviceListing {
	outputs: HeadphoneOutputDevice[];
	inputs: HeadphoneOutputDevice[];
	/** True when the browser held back ids or names, so the lists are not the
	 * machine's real device names. */
	names_withheld: boolean;
}

const IO_DEVICE_ACCESS_MESSAGES: Readonly<Record<Exclude<IoDeviceAccessStatus, 'listed'>, string>> =
	Object.freeze({
		not_checked: 'Audio devices have not been checked yet. Audio plays through the system default output.',
		permission_needed:
			'Device names are hidden until this app is allowed audio device access. Audio plays through the ' +
			'system default output until you allow it.',
		permission_denied:
			'Audio device access is blocked, so only the system default output can be listed. Allow microphone ' +
			'access for this app (browser site settings, or macOS System Settings > Privacy & Security > ' +
			'Microphone for the desktop app), then retry.',
		api_missing:
			'This browser or shell has no audio device API, so devices cannot be listed. Audio plays through ' +
			'the system default output.',
		enumeration_failed:
			'Listing audio devices failed. Audio plays through the system default output; retry to list again.',
		timeout:
			'Listing audio devices timed out. Audio plays through the system default output; retry to list again.'
	});

const IO_DEVICE_ACCESS_ACTIONS: Readonly<Record<IoDeviceAccessStatus, IoDeviceAccessAction>> = Object.freeze({
	not_checked: 'retry',
	listed: 'none',
	permission_needed: 'grant',
	permission_denied: 'retry',
	api_missing: 'retry',
	enumeration_failed: 'retry',
	timeout: 'retry'
});

export const OUTPUT_PINNING_UNSUPPORTED_NOTICE =
	'This browser or shell cannot pin an output device, so MASTER and CUE follow the system default ' +
	'output. Choose the device in macOS Sound settings.';

function _assertStatus(status: unknown): asserts status is IoDeviceAccessStatus {
	if (typeof status !== 'string' || !(status in IO_DEVICE_ACCESS_ACTIONS)) {
		throw new TypeError(`unknown I/O device access status: ${String(status)}`);
	}
}

export function ioDeviceAccessMessage(status: IoDeviceAccessStatus): string | null {
	_assertStatus(status);
	if (status === 'listed') return null;
	return IO_DEVICE_ACCESS_MESSAGES[status];
}

export function ioDeviceAccessAction(status: IoDeviceAccessStatus): IoDeviceAccessAction {
	_assertStatus(status);
	return IO_DEVICE_ACCESS_ACTIONS[status];
}

function _systemDefaultOutput(): HeadphoneOutputDevice {
	return { id: SYSTEM_DEFAULT_OUTPUT_ID, label: SYSTEM_DEFAULT_OUTPUT_LABEL };
}

/** The list a panel may always draw, whatever happened to the enumeration. */
export function systemDefaultOutputOnly(): HeadphoneOutputDevice[] {
	return [_systemDefaultOutput()];
}

function _listKind(
	devices: readonly EnumeratedDevice[],
	kind: 'audiooutput' | 'audioinput',
	unnamed: string
): { listed: HeadphoneOutputDevice[]; withheld: boolean } {
	const listed: HeadphoneOutputDevice[] = [];
	let withheld = false;
	for (const device of devices) {
		if (device.kind !== kind) continue;
		if (typeof device.deviceId !== 'string' || typeof device.label !== 'string') {
			throw new TypeError(`enumerated ${kind} must carry a string deviceId and label`);
		}
		// An empty id is the browser's placeholder for "a device of this kind
		// exists, and I will not say which". It is not a device: it cannot be
		// selected, and its value collides with every select's own placeholder.
		if (device.deviceId.trim() === '') {
			withheld = true;
			continue;
		}
		if (listed.some((entry) => entry.id === device.deviceId)) continue;
		if (device.label.trim() === '') {
			withheld = true;
			const label =
				device.deviceId === SYSTEM_DEFAULT_OUTPUT_ID && kind === 'audiooutput'
					? SYSTEM_DEFAULT_OUTPUT_LABEL
					: `${unnamed} ${listed.length + 1} (name hidden)`;
			listed.push({ id: device.deviceId, label });
			continue;
		}
		listed.push({ id: device.deviceId, label: device.label });
	}
	return { listed, withheld };
}

/**
 * CUEOUT-27: the browser's `default` output is an alias of a real device the
 * same listing also names. The page sends `default` to `setSinkId` as `""`,
 * which follows that device, so MAIN on it and CUE on `default` (or the other
 * way round) are ONE physical output and must not run as two outputs. Chrome
 * gives the alias the groupId of the device it points at; name that device as
 * the alias's `physical_id` so `sameOutputDevice` sees the collision. With no
 * groupId match the alias stays its own id, as before.
 */
function _withDefaultPhysicalId(
	devices: readonly EnumeratedDevice[],
	listed: HeadphoneOutputDevice[]
): HeadphoneOutputDevice[] {
	const outputs = devices.filter((device) => device.kind === 'audiooutput');
	const groupId = outputs.find((device) => device.deviceId === SYSTEM_DEFAULT_OUTPUT_ID)?.groupId ?? '';
	if (groupId === '') return listed;
	const target = outputs.find(
		(device) =>
			device.deviceId !== SYSTEM_DEFAULT_OUTPUT_ID &&
			device.deviceId !== 'communications' &&
			device.groupId === groupId &&
			listed.some((entry) => entry.id === device.deviceId)
	);
	if (target === undefined) return listed;
	return listed.map((entry) =>
		entry.id === SYSTEM_DEFAULT_OUTPUT_ID ? { ...entry, physical_id: target.deviceId } : entry
	);
}

/**
 * Turn a raw enumeration into the lists the panel draws.
 *
 * The output list ALWAYS carries the system default: the browser's own
 * `default` entry when it names one, else a synthesized one first in the list.
 * A machine that is playing audio has a default output whether or not the
 * browser will name it, so an empty output list is never a true statement.
 */
export function listIoDevices(devices: readonly EnumeratedDevice[]): IoDeviceListing {
	if (!Array.isArray(devices)) throw new TypeError('enumerated devices must be an array');
	const outputs = _listKind(devices, 'audiooutput', 'Output device');
	const inputs = _listKind(devices, 'audioinput', 'Input device');
	const withDefault = outputs.listed.some((output) => output.id === SYSTEM_DEFAULT_OUTPUT_ID)
		? _withDefaultPhysicalId(devices, outputs.listed)
		: [_systemDefaultOutput(), ...outputs.listed];
	return {
		outputs: withDefault,
		inputs: inputs.listed,
		names_withheld: outputs.withheld || inputs.withheld
	};
}

/**
 * CUEOUT-22: the listing in the Mac app, where the shell names every output
 * and only the inputs (AUDIO IN, the calibration mic) come from the webview.
 *
 * The shell's outputs are always named, so only the webview's inputs can be
 * withheld, and that state is kept: a webview that hides its microphones
 * behind placeholders must still read as `permission_needed`, not `listed`,
 * or the panel offers no grant and AUDIO IN stays empty.
 */
export function listNativeShellDevices(
	nativeOutputs: readonly HeadphoneOutputDevice[],
	webviewDevices: readonly EnumeratedDevice[]
): IoDeviceListing {
	if (!Array.isArray(nativeOutputs)) throw new TypeError('native outputs must be an array');
	if (!Array.isArray(webviewDevices)) throw new TypeError('enumerated devices must be an array');
	const inputs = _listKind(webviewDevices, 'audioinput', 'Input device');
	return { outputs: [...nativeOutputs], inputs: inputs.listed, names_withheld: inputs.withheld };
}

function _access(
	status: IoDeviceAccessStatus,
	detail: string | null,
	outputPinning: boolean,
	notices: readonly string[]
): IoDeviceAccess {
	if (typeof outputPinning !== 'boolean') throw new TypeError('output pinning capability must be a boolean');
	return {
		status,
		action: ioDeviceAccessAction(status),
		message: ioDeviceAccessMessage(status),
		detail,
		output_pinning: outputPinning,
		notices: outputPinning ? [...notices] : [OUTPUT_PINNING_UNSUPPORTED_NOTICE, ...notices]
	};
}

export function ioDeviceAccessNotChecked(): IoDeviceAccess {
	// Pinning is unknown before the first check; `true` keeps the unsupported
	// notice from being asserted about a shell nobody has asked yet.
	return _access('not_checked', null, true, []);
}

/**
 * The access state for an enumeration that RETURNED.
 *
 * `permission` is the Permissions API answer for `microphone`, or null when
 * the browser will not be asked (WebKit has no such descriptor). A listing
 * with nothing named and no grant on record is treated as withheld too: an
 * engine that returns an empty array before a grant must not read as a
 * machine with no devices.
 */
export function ioDeviceAccessForListing(args: {
	listing: IoDeviceListing;
	permission: string | null;
	outputPinning: boolean;
	notices?: readonly string[];
}): IoDeviceAccess {
	const { listing, permission } = args;
	if (permission !== null && typeof permission !== 'string') {
		throw new TypeError('microphone permission state must be a string or null');
	}
	const nothingNamed =
		listing.inputs.length === 0 && listing.outputs.every((output) => output.label === SYSTEM_DEFAULT_OUTPUT_LABEL);
	const withheld = listing.names_withheld || (nothingNamed && permission !== 'granted');
	if (!withheld) return _access('listed', null, args.outputPinning, args.notices ?? []);
	if (permission === 'denied') return _access('permission_denied', null, args.outputPinning, []);
	return _access('permission_needed', null, args.outputPinning, []);
}

export function ioOperationTimedOut(error: unknown): boolean {
	return (
		typeof error === 'object' &&
		error !== null &&
		(error as { name?: unknown }).name === IO_OPERATION_TIMEOUT_ERROR_NAME
	);
}

/** The access state for an enumeration that did NOT return. */
export function ioDeviceAccessForFailure(args: {
	status: 'api_missing' | 'enumeration_failed' | 'timeout';
	error: unknown;
	outputPinning: boolean;
}): IoDeviceAccess {
	if (args.status !== 'api_missing' && args.status !== 'enumeration_failed' && args.status !== 'timeout') {
		throw new TypeError(`not a failure status: ${String(args.status)}`);
	}
	const detail = args.error instanceof Error ? args.error.message : String(args.error);
	return _access(args.status, detail, args.outputPinning, []);
}

/**
 * What opening the I/O view does when the browser will only name devices after
 * a microphone grant it has not been asked for yet (permission state `prompt`).
 *
 *   - `request`: ask straight away, stop the stream the moment it opens, list
 *     again. For the dev server, whose origin is fixed, so the browser keeps
 *     the grant and the operator is asked once.
 *   - `button`: never ask on open. The panel names the state and offers
 *     `Grant access to list devices`; only that click asks. For every built
 *     app, where a prompt nobody requested is not acceptable.
 *
 * The value is set in ONE place, `vite.config.ts`, by build mode. A standalone
 * test-harness vite config never sets it and reads as `button`: the mode that
 * cannot raise a prompt is the only safe meaning for "nobody said". A value
 * that IS set and is neither word is a config error and throws.
 */
export type IoDeviceAccessOnOpen = 'request' | 'button';
export const IO_DEVICE_ACCESS_ON_OPEN_VALUES: readonly IoDeviceAccessOnOpen[] = Object.freeze(['request', 'button']);

export function ioDeviceAccessOnOpen(configured: unknown): IoDeviceAccessOnOpen {
	if (configured === undefined) return 'button';
	if (configured !== 'request' && configured !== 'button') {
		throw new TypeError(
			`VITE_IO_DEVICE_ACCESS_ON_OPEN must be one of ${IO_DEVICE_ACCESS_ON_OPEN_VALUES.join(', ')}, got ${String(configured)}`
		);
	}
	return configured;
}

/** True when an access request ended without a grant and without a fault: the
 * operator said no, closed the prompt, or left it unanswered until the
 * operation timed out. The named state already tells them what to do, so this
 * is not reported as an error. Any other rejection is a real failure. */
export function ioDeviceAccessRequestWasNotGranted(error: unknown): boolean {
	if (typeof error !== 'object' || error === null) return false;
	return (error as { name?: unknown }).name === 'NotAllowedError' || ioOperationTimedOut(error);
}

export interface SavedIoDevice {
	id: string;
	label: string;
}

export function assertSavedIoDevice(name: string, device: unknown): asserts device is SavedIoDevice {
	if (device === null || typeof device !== 'object') {
		throw new TypeError(`${name} must be an object, got ${String(device)}`);
	}
	const candidate = device as Record<string, unknown>;
	if (typeof candidate.id !== 'string' || candidate.id.trim() === '') {
		throw new TypeError(`${name}.id must be a non-empty string`);
	}
	if (typeof candidate.label !== 'string') throw new TypeError(`${name}.label must be a string`);
}

/**
 * What to tell the operator about the devices they picked last time.
 *
 * A saved device is matched by its browser device id, and failing that by its
 * label. The id is the stable key on one origin, but browser device ids are
 * salted per ORIGIN and the packaged shell serves on a new loopback port each
 * launch, so there the id never survives and the name is the only thing that
 * does. A saved device in neither form gets a notice naming it and the
 * fallback; a withheld listing proves nothing about presence, so the caller
 * only asks when the state is `listed`.
 */
export function savedIoDeviceIsListed(saved: SavedIoDevice, outputs: readonly HeadphoneOutputDevice[]): string | null {
	const byId = outputs.find((output) => output.id === saved.id);
	if (byId !== undefined) return byId.id;
	if (saved.label.trim() === '') return null;
	return outputs.find((output) => output.label === saved.label)?.id ?? null;
}

export function savedIoDeviceNotices(args: {
	saved: { master: SavedIoDevice | null; cue: SavedIoDevice | null };
	outputs: readonly HeadphoneOutputDevice[];
	selectedMasterId: string | null;
	selectedCueId: string | null;
}): string[] {
	const notices: string[] = [];
	const roles: ReadonlyArray<['MASTER' | 'HEADPHONE CUE', SavedIoDevice | null, string | null]> = [
		['MASTER', args.saved.master, args.selectedMasterId],
		['HEADPHONE CUE', args.saved.cue, args.selectedCueId]
	];
	for (const [role, saved, selectedId] of roles) {
		if (saved === null) continue;
		const name = saved.label.trim() === '' ? saved.id : saved.label;
		const listedId = savedIoDeviceIsListed(saved, args.outputs);
		if (listedId === null) {
			notices.push(`Saved ${role} output "${name}" is not connected. Using the system default output.`);
		} else if (selectedId !== listedId) {
			notices.push(`Saved ${role} output "${name}" is connected but not in use. Pick it below to use it.`);
		}
	}
	return notices;
}
