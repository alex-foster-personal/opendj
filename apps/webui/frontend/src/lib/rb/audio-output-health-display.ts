/**
 * Pure formatting for the 1px "output to device" bar under master volume
 * (pin 93c82bb36eb7): what color/visibility class to use and what the hover
 * explainer says, given the merged browser + OS delivery snapshot.
 */
import type { AudioOutputHealthSnapshot } from './audio-output-health.svelte';
import type { LivenessVerdict } from './audio-output-liveness';
import { DEVICE_NOT_DELIVERING_TOAST } from './device-output-delivering';

export interface AudioOutputHealthDisplay {
	/** CSS state class for the bar: hidden while idle, otherwise a color. */
	cssClass: 'idle' | 'ok' | 'dead' | 'unknown';
	/** Hover explainer text (also used as the element's `title`). */
	title: string;
	/** Toast text when the combined verdict is not_delivering, else null. */
	toast: string | null;
	/** Whether Switch output is actionable (macOS shell probe available). */
	switchOutputAvailable: boolean;
}

const BASE_EXPLAINER =
	'Output-to-device: whether audio reaching this context is actually being ' +
	'delivered by the output device, not just whether the app thinks it is playing.';

function describeBrowserLiveness(
	snapshot: NonNullable<AudioOutputHealthSnapshot['browser']>
): string {
	const verdict: LivenessVerdict = snapshot.verdict;
	if (verdict === 'ok') {
		return (
			`Browser binding OK - the device reports ${snapshot.output_latency_ms}ms of output latency, ` +
			'which only a device actually consuming audio produces.'
		);
	}
	if (verdict === 'stalled') {
		return (
			'Browser binding BROKEN - the device output position stopped advancing while a deck is playing. ' +
			'Recovery is running (re-bind, then a fresh audio graph).'
		);
	}
	if (verdict === 'dead' || verdict === 'dead-escalated') {
		const rebindNote =
			verdict === 'dead-escalated'
				? ' A re-bind was already attempted and did not restore it - reload the page (Cmd+R) to rebuild the audio graph.'
				: ' Attempting an automatic re-bind now.';
		return (
			'Browser binding BROKEN - context running, a deck playing, but the output reports no device ' +
			`latency: the browser is rendering into a dead output.${rebindNote}`
		);
	}
	return 'Browser liveness is idle.';
}

export function describeAudioOutputHealth(
	snapshot: AudioOutputHealthSnapshot | null
): AudioOutputHealthDisplay {
	if (!snapshot) {
		return {
			cssClass: 'unknown',
			title: `${BASE_EXPLAINER} No reading yet - the audio graph has not been built.`,
			toast: null,
			switchOutputAvailable: false
		};
	}
	const combined = snapshot.combined_verdict;
	if (combined === 'idle') {
		return {
			cssClass: 'idle',
			title: `${BASE_EXPLAINER} Nothing is playing right now, so there is nothing to check.`,
			toast: null,
			switchOutputAvailable: snapshot.device?.probe_available === true
		};
	}
	if (combined === 'unknown') {
		const reason = snapshot.device?.reason ?? 'OS output probe unavailable on this platform.';
		return {
			cssClass: 'unknown',
			title: `${BASE_EXPLAINER} Unknown - ${reason}`,
			toast: null,
			switchOutputAvailable: snapshot.device?.probe_available === true
		};
	}
	if (combined === 'not_delivering') {
		const reason =
			snapshot.device?.reason ??
			'The macOS default output device is alive but not delivering sound.';
		return {
			cssClass: 'dead',
			title: `${BASE_EXPLAINER} BROKEN - ${reason}`,
			toast: DEVICE_NOT_DELIVERING_TOAST,
			switchOutputAvailable: snapshot.device?.probe_available === true
		};
	}
	if (combined === 'ok') {
		const browserNote = snapshot.browser ? describeBrowserLiveness(snapshot.browser) : '';
		const deviceName = snapshot.device?.default_device_name;
		const deviceNote = deviceName ? ` OS probe: ${deviceName} is delivering.` : ' OS probe: delivering.';
		return {
			cssClass: 'ok',
			title: `${BASE_EXPLAINER} OK - ${browserNote}${deviceNote}`,
			toast: null,
			switchOutputAvailable: snapshot.device?.probe_available === true
		};
	}
	const _exhaustive: never = combined;
	throw new Error(`unknown combined output health verdict: ${String(_exhaustive)}`);
}
