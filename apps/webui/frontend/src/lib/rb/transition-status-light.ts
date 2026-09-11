/**
 * Pluggable Bluetooth / USB status-light adapter for the transition indicator
 * (TRANS-01, issue #324).
 *
 * This module is the documented adapter interface. Hardware backends
 * (WebBluetooth, WebUSB, HID) are not implemented in this change. Register a
 * backend with `registerTransitionStatusLight`; with nothing registered the
 * default `none` adapter stays inert and describes that there is no device.
 *
 * `setState` must not throw. A missing dongle is not an error path.
 */

import type { TransitionState } from './transition-classifier';

export interface TransitionStatusLight {
	/** Stable id, e.g. 'none', 'webbluetooth', 'usb-hid'. */
	id: string;
	/** One sentence for the TopBar tooltip. */
	describe(): string;
	/** Apply the current transition state. Must not throw. */
	setState(state: TransitionState): void;
}

const NO_DEVICE_DESCRIBE =
	'Status light - a Bluetooth or USB indicator that lights for approaching and transitioning. No device connected. Hardware backends are not implemented.';

class NoDeviceStatusLight implements TransitionStatusLight {
	readonly id = 'none';
	describe(): string {
		return NO_DEVICE_DESCRIBE;
	}
	setState(_state: TransitionState): void {
		// Inert: do not fake a lit LED when no hardware is registered.
	}
}

const DEFAULT_LIGHT: TransitionStatusLight = new NoDeviceStatusLight();
let _registered: TransitionStatusLight | null = null;

export function getTransitionStatusLight(): TransitionStatusLight {
	return _registered ?? DEFAULT_LIGHT;
}

export function registerTransitionStatusLight(adapter: TransitionStatusLight): () => void {
	if (_registered !== null) {
		throw new Error('transition status light is already registered');
	}
	_registered = adapter;
	return () => {
		if (_registered !== adapter) {
			throw new Error('transition status light ownership changed');
		}
		_registered = null;
	};
}
