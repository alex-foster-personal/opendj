/**
 * Reactive store for the 1px "output to device" bar under master volume
 * (pin 93c82bb36eb7). Lives in its own `.svelte.ts` module rather than inline
 * in `audio-context-instrumentation.ts` (plain `.ts`, no runes) or the big
 * shared `stores.svelte.ts`, so a component can read the live liveness
 * verdict without either file growing a cross-concern import.
 *
 * `snapshot` is null until the audio graph exists, or once it is torn down
 * (`clearAudioOutputHealth`) - the bar must go back to "no data" rather than
 * quote a stale device from a closed context.
 */
import type { AudioOutputSnapshot } from './audio-output-liveness';
import type {
	CombinedOutputHealthVerdict,
	DeviceDeliverySnapshot
} from './device-output-delivering';

export interface AudioOutputHealthSnapshot {
	browser: AudioOutputSnapshot | null;
	device: DeviceDeliverySnapshot | null;
	combined_verdict: CombinedOutputHealthVerdict;
}

export const audioOutputHealth = $state<{ snapshot: AudioOutputHealthSnapshot | null }>({
	snapshot: null
});

export function setAudioOutputHealth(snapshot: AudioOutputHealthSnapshot): void {
	audioOutputHealth.snapshot = snapshot;
}

export function clearAudioOutputHealth(): void {
	audioOutputHealth.snapshot = null;
}
