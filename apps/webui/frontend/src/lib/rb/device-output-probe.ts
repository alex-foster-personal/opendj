/**
 * Poll the OS output-health probe while the app is sending audio (issue #923).
 */
import {
	type CombinedOutputHealthVerdict,
	type DeviceDeliverySnapshot,
	foldDeviceDelivery
} from './device-output-delivering';
import type { AudioOutputSnapshot, LivenessVerdict } from './audio-output-liveness';
import { SILENCE_RMS_FLOOR } from './silence-watchdog';

export const DEVICE_PROBE_POLL_MS = 2_000;
export const DEVICE_PROBE_DEBOUNCE_MS = 500;
export const DEVICE_PROBE_SLA_MS = 10_000;

// Worst-case detection is one debounce plus one poll after the suspicion; it
// must fit the 10 s acceptance SLA (issue #923, gate A), or the cadence
// constants have drifted away from the promise the UI makes.
if (DEVICE_PROBE_DEBOUNCE_MS + DEVICE_PROBE_POLL_MS > DEVICE_PROBE_SLA_MS) {
	throw new Error('device output probe cadence exceeds its detection SLA');
}

const HAL_OVERLOAD_KIND = 'hal-overload';
const HAL_OVERLOAD_MESSAGE_MARKERS = [
	'skipping cycle due to overload',
	'AudioSkywalkReadLoop overwait'
] as const;

export function isHalOverloadPerfEvent(kind: string, message: string): boolean {
	if (kind === HAL_OVERLOAD_KIND) return true;
	const lower = message.toLowerCase();
	return HAL_OVERLOAD_MESSAGE_MARKERS.some((marker) => lower.includes(marker.toLowerCase()));
}

export interface DeviceOutputProbeEffects {
	fetchHealth: () => Promise<DeviceDeliverySnapshot>;
	switchOutput: () => Promise<{ cycled: boolean; error?: string }>;
	setInterval: (fn: () => void, ms: number) => unknown;
	clearInterval: (handle: unknown) => void;
	setTimeout: (fn: () => void, ms: number) => unknown;
	clearTimeout: (handle: unknown) => void;
	now: () => number;
	onUpdate: (input: {
		browser: AudioOutputSnapshot | null;
		device: DeviceDeliverySnapshot | null;
		combined_verdict: CombinedOutputHealthVerdict;
	}) => void;
	onToast?: (message: string) => void;
	addDeviceChangeListener?: (fn: () => void) => () => void;
	subscribePerfEvents?: (fn: (kind: string, message: string) => void) => () => void;
}

export interface DeviceOutputProbeHandle {
	requestProbe: (reason: string) => void;
	/**
	 * Recompute combined verdict from the latest browser snapshot without
	 * fetching OS. A browser verdict that has just turned dead (or escalated)
	 * also requests a probe, once per edge.
	 */
	republish: () => void;
	switchOutput: () => Promise<void>;
	uninstall: () => void;
}

export function installDeviceOutputProbe(
	getBrowserSnapshot: () => AudioOutputSnapshot | null,
	getPlaying: () => boolean,
	getMasterMuted: () => boolean,
	getMasterRms: () => number,
	effects: DeviceOutputProbeEffects
): DeviceOutputProbeHandle {
	let pollHandle: unknown = null;
	let debounceHandle: unknown = null;
	let deviceProbe: DeviceDeliverySnapshot | null = null;
	let lastToastVerdict: CombinedOutputHealthVerdict | null = null;
	let lastBrowserLiveness: LivenessVerdict | null = null;
	let switchInFlight = false;

	const publish = (): void => {
		const browser = getBrowserSnapshot();
		const browserLiveness: LivenessVerdict = browser?.verdict ?? 'idle';
		// The browser context reports a dead output: ask the OS whether the
		// device itself is delivering. Owned here rather than by the caller so a
		// dead verdict that arrived before this probe was installed (it is loaded
		// lazily) is still acted on at the first publish.
		if (
			(browserLiveness === 'dead' || browserLiveness === 'dead-escalated') &&
			lastBrowserLiveness !== browserLiveness
		) {
			requestProbe('browser-liveness-dead');
		}
		lastBrowserLiveness = browserLiveness;
		const combined = foldDeviceDelivery({
			playing: getPlaying(),
			masterMuted: getMasterMuted(),
			masterRms: getMasterRms(),
			browserLiveness,
			probe: deviceProbe
		});
		effects.onUpdate({ browser, device: deviceProbe, combined_verdict: combined });
		if (combined === 'not_delivering' && lastToastVerdict !== 'not_delivering') {
			effects.onToast?.(
				'Output device is not delivering audio. Switch the macOS output to another device and back, or reconnect the headphones.'
			);
		}
		lastToastVerdict = combined;
	};

	const runProbe = (): void => {
		void effects.fetchHealth().then(
			(snapshot) => {
				deviceProbe = snapshot;
				publish();
			},
			() => {
				deviceProbe = {
					device_delivering: null,
					verdict: 'unknown',
					reason: 'OS output probe request failed',
					default_device_name: null,
					default_device_uid: null,
					io_cycles_advanced: null,
					hal_overload_recent: null,
					probe_available: false,
					checked_at: new Date().toISOString()
				};
				publish();
			}
		);
	};

	const requestProbe = (_reason: string): void => {
		effects.clearTimeout(debounceHandle);
		debounceHandle = effects.setTimeout(runProbe, DEVICE_PROBE_DEBOUNCE_MS);
	};

	const shouldPoll = (): boolean =>
		getPlaying() && !getMasterMuted() && getMasterRms() >= SILENCE_RMS_FLOOR;

	const startPoll = (): void => {
		if (pollHandle !== null) return;
		pollHandle = effects.setInterval(() => {
			if (!shouldPoll()) {
				publish();
				return;
			}
			runProbe();
		}, DEVICE_PROBE_POLL_MS);
	};

	const stopPoll = (): void => {
		effects.clearInterval(pollHandle);
		pollHandle = null;
	};

	startPoll();
	const removeDeviceChange = effects.addDeviceChangeListener?.(() => requestProbe('devicechange'));
	const removePerfListener = effects.subscribePerfEvents?.((kind, message) => {
		if (isHalOverloadPerfEvent(kind, message)) {
			requestProbe('hal-overload');
		}
	});

	return {
		requestProbe,
		republish: publish,
		switchOutput: async () => {
			if (switchInFlight) return;
			switchInFlight = true;
			try {
				const result = await effects.switchOutput();
				if (!result.cycled) {
					throw new Error(result.error ?? 'switch-output failed');
				}
				runProbe();
			} finally {
				switchInFlight = false;
			}
		},
		uninstall: () => {
			stopPoll();
			effects.clearTimeout(debounceHandle);
			removeDeviceChange?.();
			removePerfListener?.();
		}
	};
}
