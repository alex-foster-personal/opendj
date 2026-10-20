/** Worker-backed 1 s mirror publish tick (#6285, follow-up to #5886).

Chromium can throttle main-thread `setInterval` when a tab is hidden or the
display is off. A dedicated Worker timer keeps firing and asks the main thread
to publish; the payload is unchanged and staleness is still keyed to the xrun
counter inside `buildUiMirror`, not to a separate heartbeat.
*/

export const MIRROR_PUBLISH_INTERVAL_MS = 1000;

// The Worker starts ticking as soon as it loads, and terminate() is the only
// way it stops, so it needs no message protocol. Kept to one short line
// because this string ships verbatim in the /performance bundle (budget).
const WORKER_BODY = `setInterval(()=>postMessage(0),${MIRROR_PUBLISH_INTERVAL_MS})`;

export function startMirrorPublishTicker(publish: () => void): () => void {
	const url = URL.createObjectURL(new Blob([WORKER_BODY], { type: 'text/javascript' }));
	const worker = new Worker(url);
	worker.onmessage = () => publish();
	return () => {
		worker.terminate();
		URL.revokeObjectURL(url);
	};
}
