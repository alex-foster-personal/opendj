/**
 * Pure formatting for the 1px "output to device" bar under master volume
 * (pin 93c82bb36eb7): what color/visibility class to use and what the hover
 * explainer says, given the liveness snapshot from `audio-output-liveness.ts`.
 *
 * Kept pure and separate from `TopBar.svelte` so the mapping from verdict to
 * class/title is unit-testable without a component harness (per the loop
 * skill's own note that unit tests load modules by string path - this stays a
 * plain `.ts` import, no Svelte instance needed).
 */
import type { AudioOutputSnapshot, LivenessVerdict } from './audio-output-liveness';

export interface AudioOutputHealthDisplay {
	/** CSS state class for the bar: hidden while idle, otherwise a color. */
	cssClass: 'idle' | 'ok' | 'dead' | 'unknown';
	/** Hover explainer text (also used as the element's `title`). */
	title: string;
}

const BASE_EXPLAINER =
	'Output-to-device: whether audio reaching this context is actually being ' +
	'delivered by the output device, not just whether the app thinks it is playing.';

export function describeAudioOutputHealth(snapshot: AudioOutputSnapshot | null): AudioOutputHealthDisplay {
	if (!snapshot) {
		return {
			cssClass: 'unknown',
			title: `${BASE_EXPLAINER} No reading yet - the audio graph has not been built.`
		};
	}
	const verdict: LivenessVerdict = snapshot.verdict;
	if (verdict === 'idle') {
		return {
			cssClass: 'idle',
			title: `${BASE_EXPLAINER} Nothing is playing right now, so there is nothing to check.`
		};
	}
	if (verdict === 'ok') {
		return {
			cssClass: 'ok',
			title:
				`${BASE_EXPLAINER} OK - the device reports ${snapshot.output_latency_ms}ms of output latency, ` +
				'which only a device actually consuming audio produces.'
		};
	}
	if (verdict === 'stalled') {
		return {
			cssClass: 'dead',
			title:
				`${BASE_EXPLAINER} BROKEN - the device output position stopped advancing while a deck is playing. ` +
				'Recovery is running (re-bind, then a fresh audio graph).'
		};
	}
	if (verdict === 'dead' || verdict === 'dead-escalated') {
		const rebindNote =
			verdict === 'dead-escalated'
				? ' A re-bind was already attempted and did not restore it - reload the page (Cmd+R) to rebuild the audio graph.'
				: ' Attempting an automatic re-bind now.';
		return {
			cssClass: 'dead',
			title:
				`${BASE_EXPLAINER} BROKEN - context running, a deck playing, but the output reports no device ` +
				`latency: the browser is rendering into a dead output.${rebindNote} ` +
				'This checks the browser’s own binding to the device, not the device itself; a device that is ' +
				'alive but not delivering sound needs the OS-level probe tracked in issue #923.'
		};
	}
	const _exhaustive: never = verdict;
	throw new Error(`unknown output liveness verdict: ${String(_exhaustive)}`);
}
