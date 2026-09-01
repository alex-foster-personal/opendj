/**
 * Route wiring for the Open DJ own-deck observer: the browser half that owns
 * a MOUNT, as distinct from the emitter itself, which owns a STATE MACHINE.
 *
 * Split out of `deck-observer-emitter.ts` when that file crossed the 600-line
 * frontend ratchet. The seam is real rather than a size dodge, and the import
 * list is the evidence: everything here touches `window`, `document` or
 * `bootScheduler`, and after the move the emitter module references none of
 * the three. It is a pure factory again, constructible and testable with no
 * DOM at all, which is what its own tests already did.
 *
 * The division of labour:
 *   - `createDeckObserverEmitter` decides WHAT to sample and WHEN to post.
 *   - this file decides WHEN THE OBSERVER EXISTS - mount, boot window, tab
 *     visibility, the agent-facing window global, and teardown.
 *
 * Raising the allowance was the alternative and was refused: the ratchet's
 * numbers are the maintainer's, and a gate config edited because the gate went red is
 * exactly the change the merge brief prohibits.
 */

import { bootScheduler, type BootScheduler } from '$lib/rb/boot-scheduler';
import {
	createDeckObserverEmitter,
	type DeckObserverEmitterOptions
} from './deck-observer-emitter';


/** The window global name an agent drives this through, so the emitter is
 *  inspectable and flushable without a UI (agent-native parity). */
export const DECK_OBSERVER_GLOBAL = '__mdtDeckObserver';

/**
 * Start observing for the lifetime of /performance. Returns the uninstall,
 * which the caller owns, matching the other `install*` hooks that route
 * mounts.
 *
 * It lives on /performance rather than in the root layout because the deck
 * engine only exists there: off-route the audio graph is disposed, so there
 * is no deck state to observe, and a root-layout import would also drag the
 * route's DSP into every other page's bundle.
 */
export function installDeckObserverEmitter(
	options: DeckObserverEmitterOptions = {},
	scheduler: BootScheduler = bootScheduler
): () => void {
	if (typeof window === 'undefined' || typeof document === 'undefined') {
		return () => {};
	}
	// WHAT IS DEFERRED, and what deliberately is NOT (Codex #709, upheld).
	//
	// The first version deferred the whole observer. That is wrong when
	// /performance mounts while REC is already running: `bootScheduler` waits
	// BOOT_QUIET_MS (3s), then up to BOOT_IDLE_TIMEOUT_MS (1s) for an idle
	// frame, then up to DECK_LOAD_YIELD_MAX_MS (10s) for an in-flight deck
	// load. Add the interval `start()` used to wait before its first tick and
	// roughly 15s of a live set went unobserved, with a cue shorter than that
	// omitted entirely. The server cannot repair it either: `_credit` banks
	// nothing for a deck's FIRST observation, so a late start loses the gap
	// AND the interval after it.
	//
	// So the split is by cost, not by convenience. SAMPLING is a local read of
	// `queryPerformanceState()` with no network at all, so it starts at mount
	// and costs the boot window nothing. FLUSHING is a POST, so it is what
	// waits. One `GET /api/sets/recorder` does now land in the boot window,
	// which is the single request this puts back after PERF-R6 moved eight
	// out; it buys correct session attribution for every snapshot taken during
	// the window, which is the alternative this rejected. Flagged in
	// docs/perf/performance-register.md rather than assumed neutral.
	let bootWindowOpen = true;
	const emitter = createDeckObserverEmitter({
		canFlush: () => !bootWindowOpen,
		...options
	});
	let disposed = false;
	scheduler.defer('deck-observer:flush', () => {
		bootWindowOpen = false;
		// Whatever the boot window accumulated goes out as soon as it closes,
		// rather than waiting for the next tick.
		if (!disposed) void emitter.flushOnce();
	});
	emitter.start();

	// A tab going away is the case the buffer exists for: flush what it saw
	// before the page is frozen or discarded.
	const flushOnHide = (): void => {
		if (document.visibilityState !== 'hidden') return;
		// A page being frozen or discarded has no boot window left to protect,
		// so this opens the gate rather than losing what the buffer holds.
		//
		// `drain` for the same reason teardown uses it: this is a last try,
		// and `flushOnce` returns without sending if a POST is already in
		// flight. Both are fired and not awaited - a handler cannot hold a
		// page open - so draining is strictly the better of the two here: it
		// costs nothing extra if the page dies and saves the tail if it does
		// not.
		bootWindowOpen = false;
		void emitter.drain();
	};
	document.addEventListener('visibilitychange', flushOnHide);
	window.addEventListener('pagehide', flushOnHide);

	Object.defineProperty(window, DECK_OBSERVER_GLOBAL, {
		value: {
			status: emitter.status,
			tick: emitter.tick,
			flush: emitter.flushOnce,
			// Agent-native parity: teardown and pagehide both drain, so an
			// agent driving this observer needs the same verb the lifecycle
			// uses, not only the single-shot one.
			drain: emitter.drain
		},
		configurable: true,
		writable: true,
		enumerable: false
	});

	return () => {
		// TEARDOWN IS A FLUSH POINT, not merely a stop.
		//
		// A client-side route change fires neither `visibilitychange` nor
		// `pagehide`, so this is the ONLY handler that sees the observer leave.
		// Closing the gate and stopping the timer without emptying the buffer
		// therefore lost everything sampled during the boot window - and since
		// that window runs up to ~14s, a cue shorter than it disappeared from
		// the recorded set entirely, which is the exact loss this PR moved
		// sampling to mount to prevent. Codex found it on #709 as the direct
		// consequence of splitting sampling from flushing.
		//
		// Opening the gate is what `flushOnHide` already does for the same
		// reason: a page that is going away has no boot window left to protect.
		// The drain is started BEFORE `disposed` and `stop()` deliberately -
		// `stop()` only clears the interval and never cancels an in-flight
		// POST, so the request outlives the teardown and lands.
		//
		// `drain`, not `flushOnce`, and the difference is the whole point:
		// `flushOnce` returns immediately when a POST is already in flight,
		// which is right for a tick (there will be another) and wrong here
		// (there will not). A deferred boot flush still in flight while the
		// next tick appended a snapshot left that snapshot buffered forever,
		// under-counting the set with nothing dropped and no error. `drain`
		// waits the in-flight POST out and then empties what is left. Codex
		// found it on #709.
		bootWindowOpen = false;
		void emitter.drain();
		disposed = true;
		emitter.stop();
		document.removeEventListener('visibilitychange', flushOnHide);
		window.removeEventListener('pagehide', flushOnHide);
		Reflect.deleteProperty(window, DECK_OBSERVER_GLOBAL);
	};
}
