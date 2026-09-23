/**
 * Browser wiring for the OS output-device probe (issue #923, AUDIO-DEVICE-01).
 *
 * `device-output-probe.ts` is the pure, effect-injected state machine; this
 * module binds it to the real timers, `navigator.mediaDevices`, the perf ring,
 * the toast store and the output-health bar. It is reached ONLY through the
 * dynamic import in `audio-context-instrumentation.ts`, so the probe and all of
 * this wiring stay out of the first-paint "/" chunk (library bundle budget).
 * Do not import it statically from anything on the library page.
 *
 * Master mute and the toast sink come in as arguments rather than imports.
 * Master mute's module lives in a chunk nothing else the instrumentation
 * lazy-loads needs, and every such chunk is another filename in the preload
 * list Vite writes into the first-paint chunk (measured: injecting it is the
 * cheaper side). `pushToast` is passed because `stores.svelte.ts` is the
 * frontend's most-imported module and the quality ratchet caps its fan-in.
 */
import type { AudioOutputSnapshot } from './audio-output-liveness';
import { RB_API_BASE } from './api-rb';
import { setAudioOutputHealth } from './audio-output-health.svelte';
import type { DeviceDeliverySnapshot } from './device-output-delivering';
import { installDeviceOutputProbe, type DeviceOutputProbeHandle } from './device-output-probe';
import { registerDeviceOutputProbe } from './device-output-probe-control';
import { masterSilenceState } from './master-silence-report';
import { subscribePerfEvents } from './perf-event-log';

export interface AudioSwitchOutputResult {
	cycled: boolean;
	from?: string;
	via?: string;
	restored?: string;
	error?: string;
}

// The two engine calls live here rather than in `api-rb.ts` because nothing
// else uses them and `api-rb.ts` ships in the first-paint "/" chunk.

/** GET /audio/output-health - macOS shell probe proxied through the engine. */
export async function fetchAudioOutputHealth(): Promise<DeviceDeliverySnapshot> {
	const r = await fetch(`${RB_API_BASE}/api/v1/audio/output-health`, {
		headers: { Accept: 'application/json' }
	});
	if (!r.ok) throw new Error(`GET /api/v1/audio/output-health failed: HTTP ${r.status}`);
	return (await r.json()) as DeviceDeliverySnapshot;
}

/** POST /audio/switch-output - cycle default macOS output away and back. */
export async function postAudioSwitchOutput(): Promise<AudioSwitchOutputResult> {
	const r = await fetch(`${RB_API_BASE}/api/v1/audio/switch-output`, {
		method: 'POST',
		headers: { Accept: 'application/json' }
	});
	if (!r.ok) {
		const detail = await r.json().catch(() => ({}));
		return { cycled: false, error: typeof detail?.detail?.error === 'string' ? detail.detail.error : r.statusText };
	}
	return await r.json();
}

/**
 * Install the probe against the real browser, register it for the Switch
 * output action (its `uninstall` unregisters it again, so a disarmed graph's
 * probe can never be switched), and catch up on what happened while this
 * module was loading:
 * the latest browser snapshot is republished at once, so the bar does not wait
 * for the next liveness tick and a dead verdict seen before install still asks
 * for its OS probe.
 */
export function install(
	getBrowserSnapshot: () => AudioOutputSnapshot | null,
	isAnyDeckPlaying: () => boolean,
	isMasterMuted: () => boolean,
	pushToast: (message: string, kind: 'error') => void
): DeviceOutputProbeHandle {
	const installed = installDeviceOutputProbe(
		getBrowserSnapshot,
		isAnyDeckPlaying,
		isMasterMuted,
		() => masterSilenceState().rms ?? 0,
		{
			fetchHealth: fetchAudioOutputHealth,
			switchOutput: postAudioSwitchOutput,
			setInterval: (fn, ms) => setInterval(fn, ms),
			clearInterval: (handle) => clearInterval(handle as ReturnType<typeof setInterval>),
			setTimeout: (fn, ms) => setTimeout(fn, ms),
			clearTimeout: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
			now: () => performance.now(),
			onUpdate: (merged) => setAudioOutputHealth(merged),
			onToast: (message) => pushToast(message, 'error'),
			addDeviceChangeListener: (fn) => {
				const media =
					typeof navigator !== 'undefined' && navigator.mediaDevices
						? navigator.mediaDevices
						: null;
				media?.addEventListener('devicechange', fn);
				return () => media?.removeEventListener('devicechange', fn);
			},
			subscribePerfEvents: (fn) =>
				subscribePerfEvents((event) => {
					fn(event.kind, event.message);
				})
		}
	);
	const probe: DeviceOutputProbeHandle = {
		...installed,
		uninstall: () => {
			installed.uninstall();
			registerDeviceOutputProbe(null);
		}
	};
	registerDeviceOutputProbe(probe);
	if (getBrowserSnapshot() !== null) probe.republish();
	return probe;
}
