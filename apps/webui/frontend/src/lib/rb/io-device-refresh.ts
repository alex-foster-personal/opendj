/**
 * IOPIN-14: how the UI lists the audio devices without being asked to.
 *
 * Two triggers come through here: the mixer mounting, and the I/O view
 * opening. Both are READS. They enumerate and ask the Permissions API for its
 * current answer; they never open a stream, never raise a permission prompt
 * and never change a route, so they are safe without a gesture (IOPIN-03:
 * opening I/O changes nothing).
 *
 * They do not go through the performance command dispatcher, for two reasons
 * that were both live defects:
 *
 *   - The dispatcher needs a command session, which the route starts only
 *     after its first async hydration. The mount-time listing ran before that,
 *     was refused, and `runPerformanceCommandFromUi` consumed the refusal on
 *     the promise that the dispatcher had already shown it. It had not. The
 *     panel then opened on three empty menus with no word of why.
 *   - A dispatched listing holds the `headphone` command scope, so an operator
 *     pressing "allow device access" right after opening I/O was refused with
 *     "command scope headphone is busy".
 *
 * The `devicechange` handler already lists outside the dispatcher for the same
 * reason. Rescan is the operator's own command and stays on it.
 *
 * One exception to "never raise a permission prompt", and only on I/O open:
 * a build configured `request` (the dev server, see `ioDeviceAccessOnOpen`)
 * asks for device access when the browser has never been asked. Mount never
 * asks in any build, and a `button` build never asks on open either.
 */
import { rustCommandUnsupported } from '$lib/audio-engine/rust-mode.svelte';
import { ioDeviceAccessAttempts, publishIoDeviceAccessFailure } from '$lib/player/headphones';
import type { IoDeviceAccessOnOpen } from '$lib/player/io-device-access';
import { engine } from '$lib/rb/audio-engine.svelte';

export const RUST_ENGINE_DEVICE_LIST_UNAVAILABLE =
	'the Rust audio engine opens the system default output itself; device picking is not implemented ' +
	'there - see PARITY-TODO';

export interface IoDeviceRefreshDeps {
	rustUnsupported: () => boolean;
	refresh: () => Promise<unknown>;
	/** List, and ask for device access first when the browser has never been asked. */
	request: () => Promise<unknown>;
	attempts: () => number;
	publishFailure: (status: 'api_missing' | 'enumeration_failed', error: unknown) => void;
}

const BROWSER_DEPS: IoDeviceRefreshDeps = {
	rustUnsupported: () => rustCommandUnsupported('headphone_outputs_refresh'),
	refresh: () => engine.refreshHeadphoneOutputs(),
	request: () => engine.requestIoDeviceNames(),
	attempts: ioDeviceAccessAttempts,
	publishFailure: publishIoDeviceAccessFailure
};

/** List the devices and guarantee the outcome is visible in
 * `mixerState.headphones.device_access`, whatever happened. Never rejects:
 * the rejection is the state. */
export async function refreshIoDeviceList(deps: IoDeviceRefreshDeps = BROWSER_DEPS): Promise<void> {
	await _listIoDevices(deps, deps.refresh);
}

/** The I/O view opened. `onOpen` is the build's configured mode, passed in so
 * the choice is visible at the call site and both modes are testable. */
export async function listIoDevicesOnOpen(
	onOpen: IoDeviceAccessOnOpen,
	deps: IoDeviceRefreshDeps = BROWSER_DEPS
): Promise<void> {
	if (onOpen === 'request') {
		await _listIoDevices(deps, deps.request);
	} else if (onOpen === 'button') {
		await _listIoDevices(deps, deps.refresh);
	} else {
		throw new TypeError(`unknown I/O open mode: ${String(onOpen)}`);
	}
}

async function _listIoDevices(deps: IoDeviceRefreshDeps, list: () => Promise<unknown>): Promise<void> {
	if (deps.rustUnsupported()) {
		deps.publishFailure('api_missing', new Error(RUST_ENGINE_DEVICE_LIST_UNAVAILABLE));
		return;
	}
	const before = deps.attempts();
	try {
		await list();
	} catch (error) {
		// The enumeration publishes its own outcome when it gets that far. An
		// unchanged attempt count means it never did, so this rejection is the
		// only record of why the list is unread.
		if (deps.attempts() === before) deps.publishFailure('enumeration_failed', error);
	}
}
