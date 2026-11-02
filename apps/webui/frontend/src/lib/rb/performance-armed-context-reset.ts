/**
 * Breaks the audio-engine <-> performance-ipc import cycle: the engine notifies
 * when its AudioContext is torn down for a rebuild (or full dispose), and
 * performance-ipc clears armed countdown records that store absolute context times.
 */

type ArmedContextResetListener = () => void;

const _listeners = new Set<ArmedContextResetListener>();

/** Subscribe to graph-context replacement. Returns unsubscribe. */
export function onPerformanceArmedContextReset(listener: ArmedContextResetListener): () => void {
	_listeners.add(listener);
	return () => {
		_listeners.delete(listener);
	};
}

/** Called from audio-engine when the live AudioContext is dropped and rebuilt. */
export function notifyPerformanceArmedContextReset(): void {
	for (const listener of _listeners) listener();
}
