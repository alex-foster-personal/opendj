/**
 * IOPIN-12: what the live graph actually wired for a djio request.
 *
 * The engine publishes a resolution on every successful graph build and clears
 * it on every teardown, so the I/O panel's inline notice and the agent
 * ui-mirror always describe the graph that exists, never a discarded one.
 *
 * Rune module: the I/O panel reads `audioOutputStatus` reactively.
 */

import { pushToast } from '$lib/stores.svelte';
import type {
	DjOutputFallback,
	DjOutputProfile,
	DjOutputResolution
} from '$lib/rb/audio-output-topology';

export interface AudioOutputStatus {
	requested_profile: DjOutputProfile | null;
	active_profile: DjOutputProfile | null;
	fallback: DjOutputFallback | null;
}

/** Long enough to read the fix; the inline notice in I/O stays for the session. */
const DJIO_FALLBACK_TOAST_MS = 15000;
/** Groups repeats: while the toast is on screen, a rebuild counts on it rather than
 * stacking another. Once it expires the next fallback build raises it again; the
 * inline I/O notice is the surface that persists. */
const DJIO_FALLBACK_TOAST_GROUP = 'djio-stereo-fallback';

export const audioOutputStatus: AudioOutputStatus = $state({
	requested_profile: null,
	active_profile: null,
	fallback: null
});

export function publishDjOutputResolution(resolution: DjOutputResolution): void {
	audioOutputStatus.requested_profile = resolution.requested;
	audioOutputStatus.active_profile = resolution.profile;
	audioOutputStatus.fallback = resolution.fallback;
	if (resolution.fallback !== null) {
		pushToast(
			resolution.fallback.message,
			'warn',
			DJIO_FALLBACK_TOAST_MS,
			undefined,
			{},
			DJIO_FALLBACK_TOAST_GROUP
		);
	}
}

export function clearDjOutputResolution(): void {
	audioOutputStatus.requested_profile = null;
	audioOutputStatus.active_profile = null;
	audioOutputStatus.fallback = null;
}

/** Plain snapshot for the agent ui-mirror (AGENT-NATIVE parity with the I/O notice). */
export function outputTopologyMirror(): AudioOutputStatus {
	const fallback = audioOutputStatus.fallback;
	return {
		requested_profile: audioOutputStatus.requested_profile,
		active_profile: audioOutputStatus.active_profile,
		fallback: fallback === null ? null : { ...fallback }
	};
}
