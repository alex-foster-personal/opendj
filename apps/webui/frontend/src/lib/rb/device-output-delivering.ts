/**
 * Pure fold for OS-level output-device delivery (issue #923, AUDIO-DEVICE-01).
 *
 * Composes browser liveness (`audio-output-liveness.ts`) with the macOS shell
 * probe verdict. Gate C: when the browser context is dead but the OS device
 * still delivers, the browser rebind/recreate path owns recovery and this fold
 * stays idle.
 */
import type { LivenessVerdict } from './audio-output-liveness';
import { SILENCE_RMS_FLOOR } from './silence-watchdog';

export type DeviceDeliveryVerdict = 'idle' | 'ok' | 'not_delivering' | 'unknown';
export type CombinedOutputHealthVerdict = DeviceDeliveryVerdict;

export interface DeviceDeliverySnapshot {
	device_delivering: boolean | null;
	verdict: 'ok' | 'not_delivering' | 'unknown';
	reason: string | null;
	default_device_name: string | null;
	default_device_uid: string | null;
	io_cycles_advanced: boolean | null;
	hal_overload_recent: boolean | null;
	probe_available: boolean;
	checked_at: string;
}

export function foldDeviceDelivery(input: {
	playing: boolean;
	masterMuted: boolean;
	masterRms: number;
	browserLiveness: LivenessVerdict;
	probe: DeviceDeliverySnapshot | null;
}): DeviceDeliveryVerdict {
	const { playing, masterMuted, masterRms, browserLiveness, probe } = input;
	if (!playing || masterMuted || masterRms < SILENCE_RMS_FLOOR) {
		return 'idle';
	}
	if (!probe || probe.device_delivering === null || probe.verdict === 'unknown') {
		return 'unknown';
	}
	if (probe.device_delivering === false) {
		if (browserLiveness === 'dead' || browserLiveness === 'dead-escalated') {
			return 'not_delivering';
		}
		if (browserLiveness === 'ok' || browserLiveness === 'stalled') {
			return 'not_delivering';
		}
		return 'not_delivering';
	}
	if (browserLiveness === 'dead' || browserLiveness === 'dead-escalated') {
		return 'idle';
	}
	if (browserLiveness === 'ok' || browserLiveness === 'stalled') {
		return 'ok';
	}
	return 'idle';
}

export const DEVICE_NOT_DELIVERING_TOAST =
	'Output device is not delivering audio. Switch the macOS output to another device and back, or reconnect the headphones.';
