/**
 * PERFMODE-14: tear down Gig runtime when entering Library mode.
 */

import { anlzCacheEntryCount, invalidateAllAnlzCacheEntries } from '$lib/components/rb/wave/anlz-cache.svelte';
import {
	activeStemWorkerCount,
	disposeStemDecoderPools,
	pooledCount
} from '$lib/player/decode/flac-decoder-pool';
import { countRegisteredAudioContexts } from '$lib/rb/audio-context-registry';
import {
	audioContextState,
	deckPcmEstimatedBytes,
	engine,
	gigDeckGraphIsPresent
} from '$lib/rb/audio-engine.svelte';
import { audioPrefetchReadyCount, clearAudioPrefetchCache } from '$lib/rb/audio-prefetch-cache.svelte';
import { PERFORMANCE_SESSION_STORAGE_KEY } from '$lib/rb/performance-session-snapshot';

const LIBRARY_MODE_EXIT_KEY = 'mdt.library_mode_exit';

let _releasePromise: Promise<void> | null = null;
let _releasePromiseGeneration: number | null = null;
let _releaseGeneration = 0;
let _releasedGeneration: number | null = null;

export interface LibraryModeIdleProbe {
	audio_context_state: AudioContextState | 'uninitialized';
	audio_context_count: number;
	prefetch_ready_count: number;
	anlz_cache_entry_count: number;
	stem_decoder_pooled_count: number;
	deck_nodes_present: boolean;
	/** Decoded deck PCM still referenced by the engine (mix plus stems), bytes. */
	deck_pcm_bytes: number;
}

declare global {
	interface Window {
		__mdtLibraryModeIdle?: boolean;
		musicDjToolsLibraryMode?: {
			read_idle_probe(): LibraryModeIdleProbe;
		};
	}
}

export function readLibraryModeIdleProbe(): LibraryModeIdleProbe {
	const contextState = audioContextState();
	return {
		audio_context_state: contextState,
		audio_context_count: countRegisteredAudioContexts(),
		prefetch_ready_count: audioPrefetchReadyCount(),
		anlz_cache_entry_count: anlzCacheEntryCount(),
		stem_decoder_pooled_count: pooledCount() + activeStemWorkerCount(),
		deck_nodes_present: gigDeckGraphIsPresent(),
		deck_pcm_bytes: deckPcmEstimatedBytes()
	};
}

export function markLibraryModeExit(): void {
	if (typeof sessionStorage === 'undefined') return;
	sessionStorage.setItem(LIBRARY_MODE_EXIT_KEY, '1');
}

export function shouldSkipPerformanceSessionRestore(): boolean {
	if (typeof sessionStorage === 'undefined') return false;
	return sessionStorage.getItem(LIBRARY_MODE_EXIT_KEY) === '1';
}

export function consumeLibraryModeExitFlag(): void {
	if (typeof sessionStorage === 'undefined') return;
	sessionStorage.removeItem(LIBRARY_MODE_EXIT_KEY);
}

export function clearPerformanceSessionForLibraryExit(storage?: Storage): void {
	if (!shouldSkipPerformanceSessionRestore()) return;
	const sessionStorageRef = storage ?? (typeof localStorage !== 'undefined' ? localStorage : null);
	if (sessionStorageRef !== null) {
		sessionStorageRef.removeItem(PERFORMANCE_SESSION_STORAGE_KEY);
	}
}

function _publishLibraryModeIdle(): void {
	if (typeof window === 'undefined') return;
	window.__mdtLibraryModeIdle = true;
	const api = {
		read_idle_probe: () => readLibraryModeIdleProbe()
	};
	window.musicDjToolsLibraryMode = api;
}

/**
 * Called when /performance (Gig) OR Trackify mounts, so a later teardown of
 * whichever session mounted BEFORE it can detect it has been superseded and
 * skip disposing an engine the new mount has since claimed. Returns the new
 * generation so the caller can capture its own and recheck it later (Sol
 * review, PR #3676: the reverse Trackify-to-Gig teardown race needed the
 * same ownership check `releaseGigRuntime` already does for Gig-to-Trackify).
 */
export function noteGigRuntimeMounted(): number {
	_releaseGeneration += 1;
	_releasedGeneration = null;
	return _releaseGeneration;
}

/** The engine-ownership generation as of right now: compare a value captured
 * from `noteGigRuntimeMounted()`'s return against this immediately before an
 * unconditional `engine.dispose()` outside of `releaseGigRuntime` itself. */
export function currentGigRuntimeGeneration(): number {
	return _releaseGeneration;
}

export async function releaseGigRuntime(opts?: {
	disposeEngine?: () => Promise<void>;
	/** Runs when a remount claimed the engine BEFORE this release disposed it,
	 * so the caller can undo what it did in anticipation of disposal. */
	onDisposeSkipped?: () => void;
	/** Runs after disposal only while this release still owns the engine. */
	onDisposed?: () => void;
	/** Runs when a remount claims the engine WHILE `disposeEngine()` is still
	 * in flight, so a newer generation exists by the time it resolves. The
	 * disposal already happened and already reset the engine's own state
	 * (including the master) to its default, so this is distinct from
	 * `onDisposeSkipped`: there is no mute left to undo, only a default the
	 * caller may need to correct for the newer mount. */
	onDisposedAfterSupersession?: () => void;
}): Promise<void> {
	const generation = _releaseGeneration;
	if (_releasedGeneration === generation) return;
	// Only join an in-flight release for THIS SAME generation. A newer
	// generation (Gig remounted since the in-flight release started) must
	// start its own release rather than await a stale one that may already
	// be past the point of no return for disposing this generation's engine.
	if (_releasePromise !== null && _releasePromiseGeneration === generation) return _releasePromise;

	const disposeEngine = opts?.disposeEngine ?? (() => engine.dispose());

	// `ownPromise` (not the shared `_releasePromise`) is what THIS call awaits
	// and compares against in `finally`, so a concurrent newer-generation
	// call that replaces `_releasePromise` with its own promise can't have
	// its promise nulled out by this call's cleanup.
	const ownPromise: Promise<void> = (async () => {
		clearAudioPrefetchCache();
		invalidateAllAnlzCacheEntries();
		// Codex P2, PR #4034, discussion_r4133146896: `disposeStemDecoderPools()`
		// deliberately propagates its own cleanup failures, but an un-caught
		// rejection here used to abort this whole IIFE before EITHER
		// `disposeEngine()` or the mute-restoration callbacks below ever ran --
		// leaving the route hard-muted at 0 with a stale pending-master capture
		// forever, on top of whatever the stem pool failure already was. Track
		// it the same way `disposeEngine()`'s own failure is tracked below (a
		// boolean, not `!== undefined`, for the same `undefined`-as-a-real-reason
		// reason as that one), and keep going: engine disposal and restoration
		// must still be attempted, and the FIRST failure (this one, if it
		// happened) is what propagates.
		let disposeFailed = false;
		let disposeError: unknown;
		try {
			await disposeStemDecoderPools();
		} catch (error) {
			disposeFailed = true;
			disposeError = error;
		}
		// Re-check after every await: if Gig remounted (noteGigRuntimeMounted
		// bumped _releaseGeneration) while this stale release was still
		// in-flight, abort before disposing the singleton engine a live,
		// remounted Gig is now using, and before publishing idle=true over it.
		if (_releaseGeneration !== generation) {
			opts?.onDisposeSkipped?.();
			if (disposeFailed) throw disposeError;
			return;
		}
		// The real engine.dispose() resets the master to its default BEFORE
		// awaiting the AudioWorklet/AudioContext teardown that can reject, so
		// a rejection here must still run the mute-restoration callback (Sol
		// P2, PR #4034, discussion_r4129826047) -- otherwise a disposal
		// failure leaves the user's pre-Library volume, and the pending-mute
		// bookkeeping, stuck forever. The rest of a successful disposal
		// (clearing the performance session, marking this generation
		// released, publishing idle) does NOT run on failure: disposal
		// genuinely did not finish, and fail-fast means the rejection must
		// still propagate rather than being marked done.
		// A tracked boolean, not `disposeError !== undefined`: a rejection can
		// legitimately carry `undefined` as its reason, which the sentinel
		// check cannot tell apart from "no error" (Sol P1, PR #4034,
		// discussion_r4131311112) -- that silently skipped the rethrow below,
		// so a disposal failure clears the session and publishes idle exactly
		// as if it had succeeded.
		try {
			await disposeEngine();
		} catch (error) {
			// The stem-pool failure above (if any) is the earlier, root cause --
			// keep it rather than overwrite with a second, possibly consequential
			// engine-disposal failure.
			if (!disposeFailed) {
				disposeFailed = true;
				disposeError = error;
			}
		}
		if (_releaseGeneration !== generation) {
			opts?.onDisposedAfterSupersession?.();
		} else {
			opts?.onDisposed?.();
		}
		if (disposeFailed) throw disposeError;
		if (_releaseGeneration !== generation) return;
		clearPerformanceSessionForLibraryExit();
		_releasedGeneration = generation;
		_publishLibraryModeIdle();
	})();
	_releasePromise = ownPromise;
	_releasePromiseGeneration = generation;

	try {
		await ownPromise;
	} finally {
		if (_releasePromise === ownPromise) {
			_releasePromise = null;
			_releasePromiseGeneration = null;
		}
	}
}

export interface GigTeardownActions {
	hardMute(): void;
	dispose(): Promise<void>;
}

/**
 * The true pre-mute master and the write-revision it was captured at, shared
 * across every `createGigTeardownActions` INSTANCE rather than held per
 * closure (Sol review, PR #4034, discussion on library-mode-runtime.ts:210).
 *
 * A rapid Gig -> Library navigation during the double-mount transition (see
 * `noteGigRuntimeMounted`'s own doc) can unmount a SECOND Gig instance before
 * the FIRST instance's release has resumed from `disposeStemDecoderPools()`.
 * Each instance gets its own `createGigTeardownActions` closure, so if
 * `masterBeforeMute` were closure-local, the second instance's `hardMute()`
 * would call `deps.readMaster()` and get the FIRST instance's temporary `0`,
 * not the user's real volume -- permanently losing it once that instance's
 * `dispose()` "restores" 0. Sharing the capture module-wide and only taking
 * it ONCE per mute chain (never overwriting a capture already pending) means
 * every overlapping `hardMute()` protects the same true original, and
 * whichever `dispose()` actually gets to restore it (gated by the same
 * write-revision proof as before) restores the right value. Once restored,
 * the state clears so the NEXT, unrelated Gig session captures its own fresh
 * original rather than inheriting a stale one.
 *
 * `_pendingMuteGeneration` records which ownership generation (see
 * `_releaseGeneration` above) most recently called `hardMute()`. It is what
 * lets a SUPERSEDED instance's `onDisposeSkipped`/`onDisposedAfterSupersession`
 * tell "a live remount inherited my mute, and nothing else will ever restore
 * it, so I must" apart from "a NEWER teardown has ALSO muted, and that
 * teardown's own `onDisposed` will restore for real once ITS disposeEngine()
 * finishes, so restoring here would just get overwritten by that call's raw
 * reset". Without this guard, two overlapping teardowns' disposal work can
 * interleave so the stale instance's restore runs, then the newer instance's
 * own `disposeEngine()` resets the master to its bare default right after,
 * discarding the very value that was just restored (found via a real
 * interleaving in this module's own test suite, not merely hypothesized).
 */
let _pendingMasterBeforeMute: number | null = null;
let _pendingMuteWriteRevision: number | null = null;
let _pendingMuteGeneration: number | null = null;

/**
 * The Gig route's hard mute and release, paired so a superseded release can
 * undo its own mute.
 *
 * The teardown mutes first and relies on `engine.dispose()` to reset the
 * master. A remount that claims the engine before the release disposes it
 * skips that reset, so the new Gig would play into a master gain of 0 until
 * the silence watchdog cut the deck about 2 s in. An in-app Library to Gig
 * navigation does exactly that: the layout renders the page in its app-shell
 * branch and then its full-bleed branch, mounting /performance twice.
 * `onDisposeSkipped` puts back the master the mute replaced, unless something
 * already moved it off the mute. A completed disposal resets the audio engine
 * to its default master, so `onDisposed` restores the same value into the
 * inert mixer state for the next Gig mount. A remount can also land WHILE
 * `disposeEngine()` is still in flight (awaiting AudioContext closure): the
 * generation check after that await catches it, but by then disposal already
 * reset the master to its default, so `onDisposedAfterSupersession` restores
 * the pre-mute value there too, gated on the same write-revision proof.
 *
 * `masterBeforeMute` and `muteWriteRevision` themselves live in MODULE state
 * (`_pendingMasterBeforeMute`/`_pendingMuteWriteRevision`), not this
 * closure -- see that state's own doc for why: two overlapping instances of
 * this function must protect the SAME true original master.
 */
export function createGigTeardownActions(deps: {
	readMaster: () => number;
	readMasterWriteRevision: () => number;
	setMaster: (value: number) => void;
	disposeEngine: () => Promise<void>;
}): GigTeardownActions {
	// Captured at THIS instance's own hardMute(), so its later callbacks can
	// tell whether a NEWER instance has since become the mute's owner.
	let myMuteGeneration: number | null = null;
	const _restore = (): void => {
		if (_pendingMasterBeforeMute !== null) deps.setMaster(_pendingMasterBeforeMute);
		_pendingMasterBeforeMute = null;
		_pendingMuteWriteRevision = null;
		_pendingMuteGeneration = null;
	};
	return {
		hardMute: () => {
			// A pending capture is only safe to REUSE (not recapture) while
			// nothing has written the master since the mute that produced it --
			// `_pendingMuteWriteRevision` is stamped from that mute's own
			// setMaster(0) call, so if the live revision has since moved past
			// it, something wrote a REAL value in between (an external write,
			// not another overlapping hardMute(), which would have re-stamped
			// this same field). That capture is stale: nothing will ever
			// legitimately restore it as the true original anymore, so treat
			// it as abandoned and recapture the CURRENT value instead of
			// inheriting it (Sol P2, PR #4034, discussion_r4130356762) --
			// otherwise this hardMute() poisons itself with the earlier,
			// already-invalidated value.
			if (
				_pendingMasterBeforeMute !== null &&
				_pendingMuteWriteRevision !== deps.readMasterWriteRevision()
			) {
				_pendingMasterBeforeMute = null;
			}
			// Only the FIRST hardMute() since the last restore (or the staleness
			// check above) captures the original: a later, overlapping instance
			// must not treat an already-muted 0 as the value to protect.
			if (_pendingMasterBeforeMute === null) {
				_pendingMasterBeforeMute = deps.readMaster();
			}
			deps.setMaster(0);
			_pendingMuteWriteRevision = deps.readMasterWriteRevision();
			_pendingMuteGeneration = currentGigRuntimeGeneration();
			myMuteGeneration = _pendingMuteGeneration;
		},
		dispose: () =>
			releaseGigRuntime({
				disposeEngine: deps.disposeEngine,
				onDisposed: () => {
					// This callback only ever fires for the winning generation
					// (the release wasn't superseded), so a revision mismatch here
					// means something wrote the master OUTSIDE the mute mechanism
					// -- hardMute() itself always keeps the revision current for
					// whichever instance called it last. Nothing will ever
					// legitimately consume this capture, so clear it (Sol P1, PR
					// #4034, discussion_r4128777355): left populated, it would
					// poison a LATER, unrelated hardMute() on this same still-live
					// Gig, which skips capturing its own true original because
					// `_pendingMasterBeforeMute` is not null, then restores this
					// abandoned value instead of its own.
					if (_pendingMasterBeforeMute === null) return;
					if (_pendingMuteWriteRevision === deps.readMasterWriteRevision()) {
						_restore();
					} else if (_pendingMuteGeneration === myMuteGeneration) {
						_pendingMasterBeforeMute = null;
						_pendingMuteWriteRevision = null;
						_pendingMuteGeneration = null;
					}
				},
				onDisposeSkipped: () => {
					// Only act if no NEWER instance has claimed the mute since
					// mine: if one has, IT owns a live disposeEngine() call of its
					// own, and that call's own onDisposed will restore for real --
					// acting here would just be overwritten by that instance's
					// disposeEngine() resetting the master to its bare default
					// right afterward.
					if (_pendingMasterBeforeMute === null || _pendingMuteGeneration !== myMuteGeneration) return;
					if (_pendingMuteWriteRevision === deps.readMasterWriteRevision() && deps.readMaster() === 0) {
						_restore();
					} else {
						// The generation matches (this IS still the pending mute's
						// owner) but the revision does not: an external write
						// invalidated the capture with nothing else pending to
						// consume it. Clear it for the same reason as onDisposed
						// above -- a later hardMute() on this Gig must recapture
						// its own current value, not inherit this abandoned one.
						_pendingMasterBeforeMute = null;
						_pendingMuteWriteRevision = null;
						_pendingMuteGeneration = null;
					}
				},
				onDisposedAfterSupersession: () => {
					// The remount landed while disposeEngine() was still awaiting
					// (e.g. AudioContext closure), so disposal already ran and
					// already reset the engine to its default master -- there is
					// no mute left at 0 to detect the way onDisposeSkipped does.
					// The write revision is still the right proof: disposeEngine's
					// own reset does not go through the tracked setter, so an
					// unchanged revision means nothing (including a deliberate
					// write from the newer mount) touched the master since this
					// teardown's mute, and the pre-mute value is safe to restore.
					// Same newer-owner guard as onDisposeSkipped: a newer
					// instance's own disposal will restore for real.
					if (_pendingMasterBeforeMute === null || _pendingMuteGeneration !== myMuteGeneration) return;
					if (_pendingMuteWriteRevision === deps.readMasterWriteRevision()) {
						_restore();
					} else {
						_pendingMasterBeforeMute = null;
						_pendingMuteWriteRevision = null;
						_pendingMuteGeneration = null;
					}
				}
			})
	};
}

/** Test-only: allow repeated teardown assertions in one process. */
export function resetLibraryModeRuntimeForTest(): void {
	_releasePromise = null;
	_releasePromiseGeneration = null;
	_releaseGeneration = 0;
	_releasedGeneration = null;
	_pendingMasterBeforeMute = null;
	_pendingMuteWriteRevision = null;
	_pendingMuteGeneration = null;
	if (typeof window !== 'undefined') {
		window.__mdtLibraryModeIdle = false;
		delete window.musicDjToolsLibraryMode;
	}
	if (typeof sessionStorage !== 'undefined') {
		sessionStorage.removeItem(LIBRARY_MODE_EXIT_KEY);
	}
}
