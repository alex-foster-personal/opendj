// perf-lab: when did the frame that shows a change get painted?
//
// A requestAnimationFrame callback runs BEFORE that frame's style, layout and
// paint, so its time hides any rendering work the change caused (and any rAF
// work registered after it). Waiting for the NEXT rAF overshoots by a whole
// refresh interval, so even an instant control would read ~17-33 ms at 60 Hz.
// A message posted from inside the rAF callback is delivered as the next task,
// which the event loop runs only after the frame's rendering steps (style,
// layout, paint) are done: that changed frame's post-paint time. It excludes
// the compositor's and display's own latency, which no page API exposes.
// driver.mjs injects this into the page (it runs there, so it uses nothing
// from this module's scope).

/** Resolves with performance.now() once the next frame has been painted. */
export function paintedFrame() {
	return new Promise((resolve) => {
		requestAnimationFrame(() => {
			const ch = new MessageChannel();
			ch.port1.onmessage = () => resolve(performance.now());
			ch.port2.postMessage(0);
		});
	});
}

// Whether the page's own control now SHOWS the commanded state: the play
// button, the channel fader or the low EQ knob of `deck`, read from the DOM the
// Svelte components render (TransportCluster, VFader, Knob). The dispatcher's
// query can reflect a command that no control renders; timing "the next frame"
// from there would time a frame that shows nothing. false while the control is
// missing or shows something else. Injected into the page like paintedFrame.
export function renderedControl(action, deck, value) {
	const el = (id) => document.querySelector(`[data-testid="${id}"]`);
	const near = (node) => node !== null && Math.abs(Number(node.getAttribute('aria-valuenow')) - value) < 1e-6;
	if (action === 'play' || action === 'pause') {
		const b = el(`play-deck-${deck}`);
		return b !== null && b.getAttribute('aria-pressed') === String(action === 'play');
	}
	if (action === 'fader_cut' || action === 'fader_open') return near(el(`channel-${deck}-fader`));
	if (action === 'eq_low_kill' || action === 'eq_low_restore') return near(el(`knob-${deck}:low`));
	return false;
}
