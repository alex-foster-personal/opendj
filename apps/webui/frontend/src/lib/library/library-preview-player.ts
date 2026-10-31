/**
 * LIBWS-04: the Crate Desk's preview player.
 *
 * Library mode tears the Gig audio engine down (PERFMODE-14) and must not
 * build it again just to audition a track, so this player owns nothing the
 * engine owns: one private AudioContext, made on the first Play and CLOSED on
 * stop, on the track ending, on leaving the page and after a long pause. It
 * holds at most one decoded track (about 0.34 MB per second of audio, see
 * preview-cue.svelte.ts), so the idle cost of Library mode is unchanged.
 *
 * It decodes rather than streaming through an HTMLAudioElement for the
 * reason preview-cue.svelte.ts records: the media pipeline stalled forever
 * in the operator's own Chromium while fetch + decodeAudioData worked, and
 * this way the format support is exactly the decks'.
 *
 * Pure of Svelte and the DOM: the caller injects the fetch, the context
 * factory and the clock, so node tests drive it with fakes.
 */

export type LibraryPreviewStatus = 'idle' | 'loading' | 'playing' | 'paused' | 'error';

export interface LibraryPreviewSnapshot {
	stable_id: string | null;
	status: LibraryPreviewStatus;
	/** Playhead in ms from the start of the track. */
	position_ms: number;
	/** Decoded length in ms, 0 until decoded. */
	duration_ms: number;
	error: string | null;
}

/** The slice of AudioBufferSourceNode the player uses. */
export interface PreviewSource {
	buffer: unknown;
	onended: ((ev: Event) => unknown) | null;
	connect(destination: unknown): unknown;
	start(when: number, offset: number): void;
	stop(): void;
	disconnect(): void;
}

/** The slice of AudioContext the player uses. */
export interface PreviewContext {
	readonly currentTime: number;
	readonly destination: unknown;
	decodeAudioData(bytes: ArrayBuffer): Promise<{ duration: number }>;
	createBufferSource(): PreviewSource;
	resume(): Promise<void>;
	close(): Promise<void>;
}

export interface LibraryPreviewDeps {
	fetchAudio(stable_id: string, signal?: AbortSignal): Promise<ArrayBuffer>;
	createContext(): PreviewContext;
	onchange(snapshot: LibraryPreviewSnapshot): void;
	setInterval(fn: () => void, ms: number): unknown;
	clearInterval(handle: unknown): void;
	setTimeout(fn: () => void, ms: number): unknown;
	clearTimeout(handle: unknown): void;
}

interface Inflight {
	id: string;
	ratio: number | undefined;
	done: Promise<void>;
}

export interface LibraryPreviewPlayer {
	/** Play `stable_id` from `ratio` (0..1), or resume it where it paused. */
	play(stable_id: string, ratio?: number): Promise<void>;
	/** Play when stopped or paused on another track, pause when playing it. */
	toggle(stable_id: string): Promise<void>;
	pause(): void;
	/** Stop and release the context and the decoded audio. */
	stop(): void;
	snapshot(): LibraryPreviewSnapshot;
}

/** Playhead publish interval, the repo's 10 Hz readout cadence. */
export const PREVIEW_TICK_MS = 100;
/** A pause this long releases the decoded audio and the context. */
export const PREVIEW_IDLE_RELEASE_MS = 60_000;

export function createLibraryPreviewPlayer(deps: LibraryPreviewDeps): LibraryPreviewPlayer {
	let state: LibraryPreviewSnapshot = {
		stable_id: null,
		status: 'idle',
		position_ms: 0,
		duration_ms: 0,
		error: null
	};
	let ctx: PreviewContext | null = null;
	/** The context a pending load made and resumed in the click: held here so Stop
	 * or teardown closes it even while the fetch or decode is still in flight. */
	let pendingCtx: PreviewContext | null = null;
	let buffer: { duration: number } | null = null;
	let bufferId: string | null = null;
	let source: PreviewSource | null = null;
	/** Context time at which the playing source's offset 0 would have played. */
	let startedAt = 0;
	let tick: unknown = null;
	let idleRelease: unknown = null;
	/** Bumped by every play/stop, so a slow fetch or decode cannot land late. */
	let generation = 0;
	/** The decode in flight, so a second click on the same track joins it. */
	let inflight: Inflight | null = null;
	/** Aborts the in-flight preview fetch when stop, supersede or teardown runs. */
	let fetchAbort: AbortController | null = null;

	const publish = (patch: Partial<LibraryPreviewSnapshot>): void => {
		state = { ...state, ...patch };
		deps.onchange(state);
	};

	const playheadMs = (): number => {
		if (ctx === null || source === null) return state.position_ms;
		const ms = (ctx.currentTime - startedAt) * 1000;
		return Math.max(0, Math.min(state.duration_ms, ms));
	};

	const stopTick = (): void => {
		if (tick !== null) deps.clearInterval(tick);
		tick = null;
	};

	const cancelIdleRelease = (): void => {
		if (idleRelease !== null) deps.clearTimeout(idleRelease);
		idleRelease = null;
	};

	/**
	 * A cleanup that failed for any reason but "already stopped or closed" may
	 * leave audio alive, so it is never swallowed (Sol P1, PR #6189). It is
	 * logged, and shown as the preview's error unless a newer preview already
	 * owns the player (that one's own state must not be overwritten).
	 */
	const cleanupFailed = (what: string, exc: unknown): void => {
		if (exc instanceof Error && exc.name === 'InvalidStateError') return;
		console.error(`library preview: ${what} failed`, exc);
		if (ctx !== null || pendingCtx !== null) return;
		const message = exc instanceof Error ? exc.message : String(exc);
		publish({ status: 'error', error: `preview ${what} failed: ${message}` });
	};

	const closeContext = (c: PreviewContext): void => {
		let closing: Promise<void>;
		try {
			closing = Promise.resolve(c.close());
		} catch (exc) {
			closing = Promise.reject(exc);
		}
		closing.catch((exc: unknown) => cleanupFailed('cleanup', exc));
	};

	const stopSource = (): void => {
		const s = source;
		source = null;
		if (s === null) return;
		s.onended = null;
		// The context this source plays through, when stopSource() runs.
		const owner = ctx;
		try {
			s.stop();
		} catch (exc) {
			// stop() throws InvalidStateError on a source that never started; anything
			// else is reported once this cleanup has finished. When that context is
			// still the live one (a pause or a seek keeps it), the source may still be
			// audible, so the preview fails and closes it (Codex P2, PR #6189); a
			// context already released was closed, which silenced it.
			queueMicrotask(() => {
				const expected = exc instanceof Error && exc.name === 'InvalidStateError';
				if (!expected && owner !== null && owner === ctx) {
					console.error('library preview: stop failed', exc);
					fail(exc);
					return;
				}
				cleanupFailed('stop', exc);
			});
		}
		s.disconnect();
	};

	const release = (): void => {
		fetchAbort?.abort();
		fetchAbort = null;
		inflight = null;
		stopTick();
		cancelIdleRelease();
		stopSource();
		buffer = null;
		bufferId = null;
		const c = ctx;
		ctx = null;
		if (c !== null) closeContext(c);
		const p = pendingCtx;
		pendingCtx = null;
		if (p !== null && p !== c) closeContext(p);
	};

	const startAt = (offsetMs: number): void => {
		if (ctx === null || buffer === null) return;
		stopSource();
		cancelIdleRelease();
		const s = ctx.createBufferSource();
		s.buffer = buffer;
		s.connect(ctx.destination);
		const offsetS = Math.max(0, Math.min(buffer.duration, offsetMs / 1000));
		startedAt = ctx.currentTime - offsetS;
		s.onended = () => {
			// Only a natural end reaches here: stopSource() clears the handler first.
			if (source !== s) return;
			release();
			publish({ status: 'idle', position_ms: 0 });
		};
		s.start(0, offsetS);
		source = s;
		stopTick();
		tick = deps.setInterval(() => publish({ position_ms: playheadMs() }), PREVIEW_TICK_MS);
		publish({ status: 'playing', position_ms: offsetS * 1000, error: null });
	};

	const fail = (exc: unknown): void => {
		release();
		const message = exc instanceof Error ? exc.message : String(exc);
		publish({ status: 'error', error: `preview failed: ${message}` });
	};

	const play = async (stable_id: string, ratio?: number): Promise<void> => {
		// The same track already decoding: retarget its start, never fetch it twice.
		if (inflight !== null && inflight.id === stable_id) {
			if (ratio !== undefined) inflight.ratio = ratio;
			return inflight.done;
		}
		const mine = ++generation;
		if (bufferId === stable_id && buffer !== null && ctx !== null) {
			const at =
				ratio === undefined ? state.position_ms : Math.max(0, Math.min(1, ratio)) * state.duration_ms;
			const held = ctx;
			try {
				await held.resume();
			} catch (exc) {
				if (mine === generation) fail(exc);
				return;
			}
			if (mine !== generation) return;
			// The idle release can land during that await: decode again below.
			if (ctx === held && buffer !== null) {
				startAt(at);
				return;
			}
			ratio = state.duration_ms > 0 ? at / state.duration_ms : 0;
		}
		release();
		publish({ stable_id, status: 'loading', position_ms: 0, duration_ms: 0, error: null });
		// Made and resumed now, inside the click, not after the fetch: WebKit only
		// lets a context start from the user's gesture itself.
		// A browser can refuse the context or its resume synchronously: callers drop
		// this promise, so that must end in `error`, not a rejection stuck at `loading`.
		let c: PreviewContext;
		let resumed: Promise<void>;
		try {
			c = deps.createContext();
			pendingCtx = c;
			resumed = Promise.resolve(c.resume());
		} catch (exc) {
			fail(exc);
			return;
		}
		const entry: Inflight = { id: stable_id, ratio, done: Promise.resolve() };
		inflight = entry;
		const abort = new AbortController();
		fetchAbort = abort;
		// A refused resume fails at once, aborting the fetch and closing the context,
		// rather than waiting at `loading` for a fetch and decode that cannot play.
		resumed.catch((exc: unknown) => {
			if (mine !== generation || abort.signal.aborted) return;
			fail(exc);
		});
		entry.done = (async () => {
			try {
				const bytes = await deps.fetchAudio(stable_id, abort.signal);
				if (mine !== generation) return;
				const decoded = await c.decodeAudioData(bytes);
				if (mine !== generation) return;
				await resumed;
				if (mine !== generation) return;
				pendingCtx = null;
				ctx = c;
				buffer = decoded;
				bufferId = stable_id;
				inflight = null;
				publish({ duration_ms: decoded.duration * 1000 });
				startAt(Math.max(0, Math.min(1, entry.ratio ?? 0)) * decoded.duration * 1000);
			} catch (exc) {
				if (mine !== generation) return;
				if (abort.signal.aborted) return;
				fail(exc);
			} finally {
				if (fetchAbort === abort) fetchAbort = null;
				// Superseded, stopped or failed before it was adopted: close it once,
				// unless release() already did.
				if (pendingCtx === c) {
					pendingCtx = null;
					closeContext(c);
				}
				if (inflight === entry) inflight = null;
			}
		})();
		return entry.done;
	};

	const pause = (): void => {
		if (state.status !== 'playing') return;
		const at = playheadMs();
		stopTick();
		stopSource();
		publish({ status: 'paused', position_ms: at });
		cancelIdleRelease();
		idleRelease = deps.setTimeout(() => {
			idleRelease = null;
			// Keep the track and the playhead, drop the audio: Play decodes again.
			release();
		}, PREVIEW_IDLE_RELEASE_MS);
	};

	const stop = (): void => {
		generation += 1;
		release();
		publish({ stable_id: null, status: 'idle', position_ms: 0, duration_ms: 0, error: null });
	};

	const toggle = async (stable_id: string): Promise<void> => {
		if (state.stable_id === stable_id && state.status === 'playing') {
			pause();
			return;
		}
		if (state.stable_id === stable_id && state.status === 'loading') return;
		const resume = state.stable_id === stable_id && state.status === 'paused';
		const ratio =
			resume && state.duration_ms > 0 ? state.position_ms / state.duration_ms : undefined;
		await play(stable_id, resume ? ratio : 0);
	};

	return { play, toggle, pause, stop, snapshot: () => state };
}
