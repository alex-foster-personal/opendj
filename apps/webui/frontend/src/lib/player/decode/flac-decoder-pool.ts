/**
 * The page's FLAC worker decoders, checked out and put back.
 *
 * Decoders live for the PAGE, not for the load. Spawning a worker and
 * compiling the wasm costs 5-41ms cold for four decoders, and a DJ loads decks
 * all night, so paying it per load would spend a slice of the win on setup the
 * previous load already did. Bounded by the widest layout: four workers total,
 * never four per deck.
 *
 * A decoder that errors is DISCARDED rather than returned: a wasm decoder that
 * has thrown mid-stream has undefined internal state, and reusing it would let
 * one corrupt file poison every later load.
 *
 * Its own module because the policy file next door had reached the 600-line
 * ratchet, and because ownership rules (who frees what, on which exit) read
 * better beside each other than interleaved with refusal policy.
 */

/** One FLAC decoder running in its own worker. */
export interface StemFlacDecoder {
	ready: Promise<void>;
	decodeFile: (bytes: Uint8Array) => Promise<DecodedStemAudio>;
	reset: () => Promise<void>;
	free: () => Promise<void> | void;
}

/** Makes one decoder. Injected in tests; defaults to the real worker. */
export type StemFlacDecoderFactory = () => StemFlacDecoder;

/**
 * What a decode comes back as, declared structurally rather than imported.
 *
 * The vendor's own types would be a static import of the decoder package,
 * which is the one reference that would pull it into the boot bundle it is
 * dynamically imported to stay out of.
 */
export interface DecodedStemAudio {
	channelData: readonly Float32Array[];
	samplesDecoded: number;
	sampleRate: number;
	errors?: readonly unknown[];
}

const _pool: StemFlacDecoder[] = [];
const MAX_POOLED_DECODERS = 4;

/** How many decoders are parked right now. */
export function pooledCount(): number {
	return _pool.length;
}

/** Forget the pool. The workers themselves are dropped, not freed. */
export function forgetPool(): void {
	_pool.length = 0;
}

/**
 * Check out a decoder, owning it on EVERY exit.
 *
 * Both awaits can reject - a wasm compile that never finishes ready, a
 * `reset()` on a decoder whose worker has died - and a rejection escaping here
 * escapes holding a live worker thread nothing else references. The caller
 * cannot free what it never received, so cleanup belongs on the only frame
 * that ever held the object. The leak is per RETRY, not per page: a failed
 * checkout is no lane trial, so every later load strands four more.
 */
export async function takeDecoder(make: StemFlacDecoderFactory): Promise<StemFlacDecoder> {
	const pooled = _pool.pop();
	if (pooled !== undefined) {
		try {
			await pooled.reset();
		} catch (exc) {
			// Not returned to the pool: a decoder that cannot reset is not a
			// decoder, and reusing it would carry the previous stream's state.
			await freeQuietly(pooled);
			throw exc;
		}
		return pooled;
	}
	const fresh = make();
	try {
		await fresh.ready;
	} catch (exc) {
		await freeQuietly(fresh);
		throw exc;
	}
	return fresh;
}

export function returnDecoder(decoder: StemFlacDecoder): void {
	if (_pool.length >= MAX_POOLED_DECODERS) {
		void decoder.free();
		return;
	}
	_pool.push(decoder);
}

/**
 * Release a decoder without letting the release replace the real failure.
 *
 * `free()` on a decoder that never became ready can itself throw, and that
 * throw would propagate in place of the reason we are freeing it - turning a
 * reported `decode-failed` into an unhandled rejection out of the deck load.
 */
export async function freeQuietly(decoder: StemFlacDecoder): Promise<void> {
	try {
		await decoder.free();
	} catch {
		// The worker is unreachable either way; the caller's error is the news.
	}
}

/**
 * Spawn and compile up to `count` decoders BEFORE something times them.
 *
 * The lane calibration is a stopwatch over one whole load, and the worker
 * trial is the load that first builds this pool. Charging spawn plus wasm
 * compile to it measures a cost that no later load pays - every one of them
 * reuses these decoders - and the bias is not academic: on this repo's own
 * live run the stopwatch had the workers 1.71x faster while the trial that
 * paid the cold start settled the session on the main thread. That verdict is
 * permanent, so a one-time cost would have cost every deck load in the session.
 *
 * A decoder that cannot start is NOT swallowed into a silent half-warm pool:
 * warming stops at the first failure and the decoders it did get are parked,
 * so the load that follows meets the same failure through `takeDecoder` and
 * reports it per part, which is the path that already handles it.
 */
export async function warmPool(make: StemFlacDecoderFactory, count: number): Promise<void> {
	const taken: StemFlacDecoder[] = [];
	try {
		while (taken.length < count && _pool.length + taken.length < MAX_POOLED_DECODERS) {
			taken.push(await takeDecoder(make));
		}
	} catch {
		// Deliberately not rethrown, and not a masked failure: the load that
		// follows calls takeDecoder again for its first part, meets the same
		// failure, and reports it as a per-part refusal with the decoder freed.
		// Rethrowing would fail a whole deck load for a warmup that is only
		// ever an optimization.
	} finally {
		for (const decoder of taken) returnDecoder(decoder);
	}
}
