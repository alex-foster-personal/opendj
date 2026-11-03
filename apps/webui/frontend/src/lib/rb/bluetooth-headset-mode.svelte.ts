/**
 * BTHFP-8319: warn when Bluetooth headphones are in headset (call) mode, or one
 * app away from it.
 *
 * Opening a Bluetooth headset's microphone drops it from music mode (A2DP,
 * wideband stereo) to call mode (HFP, mono, 8 to 24 kHz), and macOS often
 * makes newly connected headphones the system input. The page cannot check
 * this itself without opening the mic, which would cause the flip, so the
 * engine reads CoreAudio properties at GET /api/v1/audio-headset-mode.
 *
 * The banner is dismissable. A dismissal holds only for the state it was
 * shown for: a later flip from "mic selected" to "in call mode", or another
 * pair of headphones, shows it again.
 */
import { API_BASE } from '$lib/api/base';
import type { components } from '$lib/api-types';

export type AudioHeadsetModeReport = components['schemas']['AudioHeadsetModeOut'];

export interface HeadsetModeWarning {
	/** Identifies the state the warning describes, so a dismissal is scoped to it. */
	key: string;
	text: string;
	/** Hover text with the measured numbers behind the warning. */
	title: string;
}

export const HEADSET_MODE_FIX =
	'To keep full quality, Option-click the sound icon in the menu bar and choose another Input, ' +
	'or change Input in System Settings > Sound.';

/** The banner copy, or null when there is nothing to say. Pure, so the copy
 * is testable without a server. An unsupported report says nothing: a probe
 * that could not run is neither a warning nor an all-clear. */
export function headsetModeWarning(report: AudioHeadsetModeReport | null): HeadsetModeWarning | null {
	if (report === null || !report.supported || report.output === null || report.output === undefined) {
		return null;
	}
	const output = report.output;
	const name = output.name.trim() === '' ? 'Your Bluetooth headphones' : output.name;
	const rate =
		`${name} output runs at ${output.nominal_sample_rate_hz} Hz; ` +
		'music mode runs at 44100 or 48000 Hz, call mode at 8000 to 24000 Hz.';
	if (report.verdict === 'headset_mode') {
		return {
			key: `headset_mode:${output.name}`,
			text:
				`${name} switched to headset (call) mode because an app is using its microphone, ` +
				`so it sounds tinny. ${HEADSET_MODE_FIX}`,
			title: rate
		};
	}
	if (report.verdict === 'mic_selected') {
		const input = report.input?.name ?? name;
		return {
			key: `mic_selected:${output.name}`,
			text:
				`${name} microphone is the Mac's sound input, so any app that listens will switch ` +
				`the headphones to tinny headset (call) mode. ${HEADSET_MODE_FIX}`,
			title: `System input: ${input}. ${rate}`
		};
	}
	return null;
}

// Through API_BASE like the shared client, so VITE_API_BASE reaches the same daemon.
const AUDIO_HEADSET_MODE_PATH = `${API_BASE}/api/v1/audio-headset-mode`;
export const HEADSET_MODE_POLL_MS = 5_000;

export const bluetoothHeadsetMode = $state<{
	report: AudioHeadsetModeReport | null;
	dismissedKey: string | null;
}>({
	report: null,
	dismissedKey: null
});

export function dismissHeadsetModeWarning(key: string): void {
	bluetoothHeadsetMode.dismissedKey = key;
}

export async function refreshBluetoothHeadsetMode(fetcher: typeof fetch = fetch): Promise<void> {
	try {
		const res = await fetcher(AUDIO_HEADSET_MODE_PATH);
		const report: AudioHeadsetModeReport = res.ok
			? ((await res.json()) as AudioHeadsetModeReport)
			: { supported: false, verdict: 'unsupported', output: null, input: null, error: `HTTP ${res.status}` };
		bluetoothHeadsetMode.report = report;
		// A dismissal covers one episode: once a real reading shows that state has
		// ended (clear, or a different state), the next occurrence warns again. A
		// failed read proves nothing either way, so it keeps the dismissal.
		if (report.supported && headsetModeWarning(report)?.key !== bluetoothHeadsetMode.dismissedKey) {
			bluetoothHeadsetMode.dismissedKey = null;
		}
	} catch (error) {
		// Advisory only: a failed check hides the banner, it never blocks playback.
		bluetoothHeadsetMode.report = {
			supported: false,
			verdict: 'unsupported',
			output: null,
			input: null,
			error: error instanceof Error ? error.message : String(error)
		};
	}
}

/** Poll while the page is visible; returns the stop function. */
export function startBluetoothHeadsetModePolling(intervalMs: number = HEADSET_MODE_POLL_MS): () => void {
	const tick = (): void => {
		if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return;
		void refreshBluetoothHeadsetMode();
	};
	tick();
	const timer = setInterval(tick, intervalMs);
	return () => clearInterval(timer);
}
