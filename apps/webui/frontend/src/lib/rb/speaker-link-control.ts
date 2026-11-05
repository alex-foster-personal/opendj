/**
 * AUDIO-DEVICE-05: the room graph's recreate hook, registered by the audio
 * engine's watchdog arm and read by the speaker-link runtime. Import-free on
 * purpose: `audio-context-instrumentation.ts` is on the engine's eager path,
 * and the speaker-link runtime (prefs, fetches, the toast copy) must stay out
 * of that chunk, so only this registry is imported there.
 */

let _recreateGraph: (() => Promise<void>) | null = null;

export function registerSpeakerLinkGraph(recreateGraph: (() => Promise<void>) | null): void {
	_recreateGraph = recreateGraph;
}

/** Recreate the room AudioContext with the loaded decks re-attached. */
export function recreateRoomGraph(): Promise<void> {
	if (_recreateGraph === null) {
		return Promise.reject(new Error('no room audio graph is armed to recreate'));
	}
	return _recreateGraph();
}
