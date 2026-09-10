/**
 * Q18 rung 1: decode a stem bundle's FLAC parts in real Web Workers.
 *
 * WebKit runs `decodeAudioData` on ONE decode thread, so the four-way
 * `decodeStems` site does not parallelize no matter how it is awaited -
 * measured at 1.27x / 1.30x / 0.99x concurrent-vs-sequential across three real
 * bundles. A WASM decoder in four workers does parallelize (1.55x / 3.28x /
 * 1.83x) and lands the whole 4-way decode 1.7x to 2.0x faster (855/1139/695ms
 * against 1459/1942/1372ms). Worker spawn plus wasm compile for four decoders
 * measured 5-41ms cold, so there is no cold-start objection left standing.
 * Numbers, method and the rejected rungs:
 * docs/perf/research/stems-decode-rung-bench.md.
 *
 * This module is the POLICY. It owns three refusals, each of which exists
 * because taking the fast path anyway would be worse than being slow:
 *
 * 1. NOT FLAC -> `decodeAudioData`. v2 bundles are FLAC by server contract
 *    (`_require_flac_magic`), but a v3 bundle may ship any one codec, and this
 *    decoder is FLAC-only. Sniffed from the bytes rather than from a manifest
 *    field, so it cannot disagree with what was actually served.
 * 2. WRONG SAMPLE RATE -> `decodeAudioData`. This is the subtle one.
 *    `decodeAudioData` RESAMPLES to the context rate; a WASM decoder returns
 *    the file's own rate untouched. A 48kHz stem decoded through the fast path
 *    into a 44.1kHz context would produce a buffer that is the right length in
 *    SAMPLES and the wrong length in SECONDS. `validateStemBufferAlignment`
 *    compares the parts to EACH OTHER and would pass all four happily; the
 *    engine's own stem/source check catches it one step later and FAILS the
 *    load. So the cost of not refusing here is a stemmed deck that refuses to
 *    load on a bundle `decodeAudioData` would have resampled correctly - a
 *    regression, not a drift.
 * 3. DECODER UNAVAILABLE -> `decodeAudioData`. No worker support, a blocked
 *    wasm compile, a decode error: the deck still loads.
 * 4. DECODER REPORTED ERRORS -> `decodeAudioData`. `decodeFile` RESOLVES on a
 *    damaged stream, handing back the frames it managed plus an `errors`
 *    array, so a truncated or corrupt part would otherwise be published as
 *    audio. Nonempty errors are a refusal AND discard the decoder, because a
 *    wasm decoder that reported a stream error has undefined internal state.
 *
 * Every refusal is REPORTED, never silent - the caller receives which path
 * each part took and why, so a load that quietly lost its 1.7x says so in the
 * perf ring instead of looking like a slow machine.
 */

/** `fLaC`, the FLAC stream marker. Same four bytes the server enforces. */
const FLAC_MAGIC = [0x66, 0x4c, 0x61, 0x43] as const;

/**
 * The decoded shape, declared structurally rather than imported.
 *
 * The decoder package is loaded dynamically (see `_defaultDecoderFactory`), so
 * importing its types here would be the one static reference that pulls it
 * back into the boot bundle.
 */
export interface DecodedStemAudio {
	channelData: Float32Array<ArrayBufferLike>[];
	samplesDecoded: number;
	sampleRate: number;
	/**
	 * What the decoder itself reported wrong, if anything.
	 *
	 * `decodeFile` RESOLVES on a damaged stream: libFLAC's error and state codes
	 * are collected here and returned ALONGSIDE whatever frames did decode.
	 * Omitting the field would make a partial decode indistinguishable from a
	 * whole one. Typed as unknown rather than the vendor's `DecodeError`, whose
	 * import is the one static reference that would pull the decoder package
	 * back into the boot bundle; nothing here reads a field of it.
	 */
	errors?: readonly unknown[];
}

/** One FLAC decoder running in its own worker. */
export interface StemFlacDecoder {
	ready: Promise<void>;
	decodeFile: (bytes: Uint8Array) => Promise<DecodedStemAudio>;
	reset: () => Promise<void>;
	free: () => Promise<void> | void;
}

/** Makes one decoder. Injected in tests; defaults to the real worker. */
export type StemFlacDecoderFactory = () => StemFlacDecoder;

/** Why a part did NOT take the worker path. `null` means it did. */
/**
 * Which lane a part took, and why.
 *
 * Three of these are not refusals at all. `calibrating` is one of the two trial
 * loads a session runs to find out which lane is faster on THIS engine;
 * `engine-prefers-main` is every load after that measurement said the workers
 * would be slower here; `awaiting-calibration` is a load that ran while the
 * session had no verdict AND could not be a trial itself - it overlapped
 * another load, or its bundle was not FLAC. It is distinct from
 * `engine-prefers-main` because nothing was measured, and a ring row saying an
 * engine preferred the main thread when no comparison ever ran would be a
 * verdict invented out of a scheduling accident. See `_claimLane`.
 */
export type StemDecodeRefusal =
	| 'not-flac'
	| 'sample-rate-mismatch'
	| 'decoder-unavailable'
	| 'decode-failed'
	| 'empty-decode'
	| 'calibrating'
	| 'awaiting-calibration'
	| 'engine-prefers-main';

/** What one part cost and which path produced it. */
export interface StemPartDecodeReport {
	part: string;
	viaWorker: boolean;
	refusal: StemDecodeRefusal | null;
	ms: number;
	/** Encoded size, so two lanes running different parts stay comparable. */
	bytes: number;
}

export function isFlacContainer(bytes: ArrayBuffer): boolean {
	if (bytes.byteLength < FLAC_MAGIC.length) return false;
	const head = new Uint8Array(bytes, 0, FLAC_MAGIC.length);
	return FLAC_MAGIC.every((byte, i) => head[i] === byte);
}

/**
 * Turn a WASM decode into an AudioBuffer on this context.
 *
 * Refuses a rate the context will not honor rather than producing a buffer
 * that is right in samples and wrong in seconds - see refusal 2 above.
 *
 * Refuses a decode the decoder itself complained about BEFORE looking at the
 * samples, because a damaged stream resolves with real-looking channel data at
 * the right rate and a short frame count. Every check below would pass it.
 */
export function stemAudioBuffer(
	ctx: BaseAudioContext,
	decoded: DecodedStemAudio
): AudioBuffer | StemDecodeRefusal {
	if (decoded.errors !== undefined && decoded.errors.length > 0) return 'decode-failed';
	if (decoded.samplesDecoded <= 0 || decoded.channelData.length === 0) return 'empty-decode';
	if (decoded.sampleRate !== ctx.sampleRate) return 'sample-rate-mismatch';
	const buffer = ctx.createBuffer(
		decoded.channelData.length,
		decoded.samplesDecoded,
		decoded.sampleRate
	);
	for (let channel = 0; channel < decoded.channelData.length; channel++) {
		// The decoder may report more samples than a short final block filled;
		// copyToChannel would throw on a source longer than the buffer.
		// The decoder types its output over ArrayBufferLike because a decoder in
		// general may hand back a SharedArrayBuffer view. This one cannot: its
		// output crosses a worker boundary by structured clone, which produces
		// a plain ArrayBuffer. Narrowed once, here, rather than copying ~30MB
		// per channel to satisfy the type.
		const source = decoded.channelData[channel] as Float32Array<ArrayBuffer>;
		buffer.copyToChannel(
			source.length > decoded.samplesDecoded ? source.subarray(0, decoded.samplesDecoded) : source,
			channel
		);
	}
	return buffer;
}

/**
 * The real worker decoder, imported at first use.
 *
 * A dynamic import against the house "no inline imports" rule, deliberately:
 * this package is ~200KB of JS plus a wasm blob, and every module in the
 * static graph reachable from `audio-engine.svelte.ts` is in the BOOT bundle.
 * A decoder that is only ever needed when a stemmed deck loads must not be
 * paid for at app start - that cost would land on exactly the boot-latency
 * budget this program is elsewhere trying to reduce.
 */
async function _defaultDecoderFactory(): Promise<StemFlacDecoderFactory> {
	const mod = await import('@wasm-audio-decoders/flac');
	return (): StemFlacDecoder => new mod.FLACDecoderWebWorker();
}

export interface StemDecodeOptions {
	/** Injected in tests. Omitted in the app, which loads the real worker. */
	makeDecoder?: StemFlacDecoderFactory;
	/** Injected in tests so a fallback is observable without a real context. */
	decodeFallback?: (bytes: ArrayBuffer) => Promise<AudioBuffer>;
	now?: () => number;
}

export interface StemDecodeResult<P extends string> {
	buffers: Record<P, AudioBuffer>;
	reports: StemPartDecodeReport[];
}

/**
 * Decode every part, each in its own worker where the bytes allow it.
 *
 * Parts are started together and awaited together: the whole point of the rung
 * is that four workers overlap, so a sequential loop here would spend the
 * dependency and keep the baseline.
 *
 * A part that refuses the worker path falls back INDIVIDUALLY rather than
 * condemning the bundle - a v3 bundle is single-codec by server contract, so
 * in practice this is all-or-nothing anyway, but a per-part rule cannot be
 * wrong about a bundle that is not.
 */
export async function decodeStemParts<P extends string>(
	ctx: BaseAudioContext,
	encoded: Partial<Record<P, ArrayBuffer>>,
	parts: readonly P[],
	options: StemDecodeOptions = {}
): Promise<StemDecodeResult<P>> {
	const now = options.now ?? (() => performance.now());
	const fallback =
		options.decodeFallback ??
		((bytes: ArrayBuffer): Promise<AudioBuffer> => ctx.decodeAudioData(bytes));

	// Claimed before the first await, so two loads racing into this function
	// cannot both become the same lane's trial. Released in the `finally`
	// below on EVERY exit, including a rejecting fallback: a claim that leaked
	// would mark the session permanently mid-trial and freeze the lane.
	const claim = _claimLane(parts.every((part) => _flacBytes(encoded[part])));
	let claimReleased = false;
	try {
		// Resolved ONCE for the bundle, not once per part: four concurrent
		// dynamic imports of the same module are four chances to pay the
		// resolve twice.
		let makeDecoder: StemFlacDecoderFactory | null = options.makeDecoder ?? null;
		if (makeDecoder === null && claim.lane === 'workers') {
			try {
				makeDecoder = await _defaultDecoderFactory();
			} catch {
				makeDecoder = null;
			}
		}

		const loadStarted = now();
		// Snapshotted at load start, not read per part after the decode: the
		// session verdict can settle while this load is decoding, and a load
		// that ran before any comparison existed must not report the verdict
		// that arrived after it.
		const mainThreadRefusal = _mainThreadRefusal(claim);
		const reports: StemPartDecodeReport[] = [];
		// allSettled, NOT all. `Promise.all` rejects on the FIRST rejection while
		// its siblings keep running - they cannot be cancelled - so the `finally`
		// would release the claim with decodes still in flight. The next load
		// would read an uncontended machine and time a trial against those
		// orphans: the contamination the claim exists to prevent, arriving
		// through the error path rather than the happy one.
		const settled = await Promise.allSettled(
			parts.map(async (part) => {
				const started = now();
				const bytes = encoded[part] as ArrayBuffer;
				// BOTH read before any decode touches the buffer.
				// `decodeAudioData` DETACHES the ArrayBuffer it is given, so a
				// byte read afterwards sees a zero-length buffer. Written inline
				// where it is used, the sniff below reported `not-flac` for real
				// FLAC, made every calibration trial unclean, and stopped the
				// session ever settling a lane. No unit test saw it - their
				// fallback does not detach - and the live browser run did.
				const byteLength = bytes === undefined ? 0 : bytes.byteLength;
				const isFlac = _flacBytes(bytes);
				const outcome =
					claim.lane === 'main-thread'
						? {
								// Sniffed on this lane too. A part that is not FLAC could
								// never have taken the worker path, and reporting the lane
								// reason instead would hide the permanent cause behind a
								// per-session one.
								buffer: await fallback(bytes),
								refusal: isFlac ? mainThreadRefusal : 'not-flac'
							}
						: await _decodeOnePart(ctx, bytes, makeDecoder, fallback);
				reports.push({
					part,
					viaWorker: outcome.refusal === null,
					refusal: outcome.refusal,
					ms: Math.round(now() - started),
					bytes: byteLength
				});
				return [part, outcome.buffer] as const;
			})
		);
		// Every part has now settled, so nothing is still consuming a decoder
		// when the claim is released. The first failure is rethrown unchanged.
		const failure = settled.find((result) => result.status === 'rejected');
		if (failure !== undefined) throw (failure as PromiseRejectedResult).reason;
		const decoded = settled.map(
			(result) => (result as PromiseFulfilledResult<readonly [P, AudioBuffer]>).value
		);
		reports.sort((a, b) => parts.indexOf(a.part as P) - parts.indexOf(b.part as P));
		// Only a CLEAN run of the lane counts. A load where some part refused
		// the worker path for its own reason did not measure that lane.
		const clean =
			claim.lane === 'main-thread'
				? reports.every((r) => r.refusal === 'calibrating')
				: reports.every((r) => r.refusal === null);
		_releaseLane(
			claim,
			clean,
			reports.reduce((sum, r) => sum + r.bytes, 0),
			now() - loadStarted
		);
		claimReleased = true;
		return { buffers: Object.fromEntries(decoded) as Record<P, AudioBuffer>, reports };
	} finally {
		if (!claimReleased) _releaseLane(claim, false, 0, 0);
	}
}

function _flacBytes(bytes: ArrayBuffer | undefined): boolean {
	return bytes !== undefined && isFlacContainer(bytes);
}

/**
 * Why a main-thread load took the main thread - measuring, or measured.
 *
 * The third case is the one worth naming: a load that ran the shipping lane
 * while the session had no verdict at all. Calling that `engine-prefers-main`
 * would report a comparison that never happened.
 */
function _mainThreadRefusal(claim: LaneClaim): StemDecodeRefusal {
	if (claim.trialing) return 'calibrating';
	return _preferredLane === null ? 'awaiting-calibration' : 'engine-prefers-main';
}

// ------------------------------------------------------------- lane choice

/**
 * WHICH lane is faster is a property of the engine, and it is not settled.
 *
 * The rung exists because WebKit runs `decodeAudioData` on one decode thread,
 * so four concurrent calls buy ~1.0-1.3x while four workers buy 1.7-2.0x.
 * Measured on this repo's own fixtures (`pnpm test:live:stem-decode-workers`):
 * WebKit 909ms main-thread vs 556ms in workers, 1.63x FASTER; Chromium 266ms
 * vs 462ms, 1.7x SLOWER. Chromium's decoder does not have the limit this rung
 * routes around, so there the rung is a regression.
 *
 * Hard-coding either answer pins a VALUE that rots the next time an engine
 * ships a decoder change - and one already did, which is why the bench's own
 * Chrome column disagrees with the number above. So the first two stemmed
 * loads of a session MEASURE: load one runs entirely on the main thread, load
 * two entirely in workers, each normalized to encoded bytes per wall
 * millisecond, and the winner holds the lane for the rest of the session.
 *
 * WHOLE loads, one lane each, rather than splitting a single load between the
 * lanes. The split version was written first and was WRONG: a main-thread
 * decode running alongside three saturated workers is starved by them, so the
 * main-thread lane measured slow for a reason that had nothing to do with the
 * question, and Chromium calibrated to the lane that is 1.7x slower there.
 * Caught by the live test's cross-check against a separately timed comparison
 * - the calibration and the stopwatch disagreed, and the stopwatch was right.
 */
type DecodeLane = 'workers' | 'main-thread';

/** Encoded bytes per wall millisecond, per lane, until one wins. */
const _laneTrials = new Map<DecodeLane, number>();
let _preferredLane: DecodeLane | null = null;

/** How much faster the workers must be to win, so noise cannot flip the choice. */
const LANE_MARGIN = 1.25;

/** The lane being trialed right now, or null when no trial is in flight. */
let _trialLane: DecodeLane | null = null;
/** Set when any other load overlapped the trial in flight. */
let _trialContended = false;
/** Loads decoding right now, trial or not. */
let _loadsInFlight = 0;

/** Which lane a load runs, and whether its wall time is a measurement. */
interface LaneClaim {
	lane: DecodeLane;
	trialing: boolean;
}

/**
 * Claim this load's lane, SYNCHRONOUSLY, before the function's first await.
 *
 * Deck loads are explicitly allowed to overlap, and the calibration is a
 * stopwatch, so three things have to hold that a plain "which lane has no
 * trial yet" read does not give:
 *
 * 1. RESERVED, not inferred. Two loads starting in the same tick would both
 *    read "main-thread has never been trialed" and both become that trial.
 * 2. CONTENDED trials are discarded. A decode sharing the machine with another
 *    measures contention, not the lane - and the failure is not symmetric
 *    noise: two overlapping main-thread loads both measure slow, so a later
 *    uncontended worker trial wins by default, which is how Chromium, where
 *    the workers are 1.7x SLOWER, could pin itself to them for the session.
 * 3. Only COMPARABLE bundles are trialed. The worker lane can only ever trial
 *    FLAC (anything else refuses per part and the trial is unclean), so a
 *    main-thread trial on an AAC or OGG v3 bundle would settle a FLAC lane
 *    from a different codec.
 *
 * A load that cannot be a trial runs the MAIN THREAD, which is what ships
 * today, rather than guessing at the unmeasured lane.
 */
function _claimLane(everyPartIsFlac: boolean): LaneClaim {
	_loadsInFlight += 1;
	if (_trialLane !== null) _trialContended = true;
	if (_preferredLane !== null) return { lane: _preferredLane, trialing: false };
	if (_trialLane !== null || _loadsInFlight > 1 || !everyPartIsFlac) {
		return { lane: 'main-thread', trialing: false };
	}
	// Main thread first: it is what ships today, so a session that only ever
	// loads one stemmed deck pays nothing for the trial.
	const lane: DecodeLane = _laneTrials.has('main-thread') ? 'workers' : 'main-thread';
	_trialLane = lane;
	_trialContended = false;
	return { lane, trialing: true };
}

/** Give the claim back, recording the trial only if it stayed measurable. */
function _releaseLane(claim: LaneClaim, clean: boolean, bytes: number, wallMs: number): void {
	_loadsInFlight -= 1;
	if (!claim.trialing) return;
	const contended = _trialContended;
	_trialLane = null;
	_trialContended = false;
	if (clean && !contended) _recordTrial(claim.lane, bytes, wallMs);
}

function _recordTrial(lane: DecodeLane, bytes: number, wallMs: number): void {
	if (_preferredLane !== null || bytes <= 0 || wallMs <= 0) return;
	_laneTrials.set(lane, bytes / wallMs);
	const main = _laneTrials.get('main-thread');
	const workers = _laneTrials.get('workers');
	if (main === undefined || workers === undefined) return;
	_preferredLane = workers > main * LANE_MARGIN ? 'workers' : 'main-thread';
}

/**
 * Decoders live for the page, not for the load.
 *
 * Spawning a worker and compiling the wasm costs 5-41ms cold for four
 * decoders, and a DJ loads decks all night. Paying that per load would spend a
 * measurable slice of the win on setup the previous load already did. The pool
 * is bounded by the widest layout (four parts), so it is four workers total,
 * never four per deck.
 *
 * A decoder that errors is DISCARDED rather than returned: a wasm decoder that
 * has thrown mid-stream has undefined internal state, and reusing it would let
 * one corrupt file poison every later load.
 */
const _pool: StemFlacDecoder[] = [];
const MAX_POOLED_DECODERS = 4;

/**
 * Check out a decoder, owning it on EVERY exit.
 *
 * Both awaits can reject - a wasm compile that never finishes ready, a
 * `reset()` on a decoder whose worker has died - and a rejection escaping this
 * function escapes holding a live worker thread nothing else references. The
 * caller cannot free what it never received, so cleanup belongs HERE, on the
 * only frame that ever held the object. The leak is per RETRY, not per page:
 * a failed checkout is not recorded as a lane trial, so every later stem load
 * tries again and strands four more workers and four more wasm heaps.
 */
async function _takeDecoder(make: StemFlacDecoderFactory): Promise<StemFlacDecoder> {
	const pooled = _pool.pop();
	if (pooled !== undefined) {
		try {
			await pooled.reset();
		} catch (exc) {
			// Not returned to the pool: a decoder that cannot reset is not a
			// decoder, and reusing it would carry the previous stream's state.
			await _freeQuietly(pooled);
			throw exc;
		}
		return pooled;
	}
	const fresh = make();
	try {
		await fresh.ready;
	} catch (exc) {
		await _freeQuietly(fresh);
		throw exc;
	}
	return fresh;
}

/**
 * Release a decoder without letting the release replace the real failure.
 *
 * `free()` on a decoder that never became ready can itself throw, and that
 * throw would propagate in place of the reason we are freeing it - turning a
 * reported `decode-failed` into an unhandled rejection out of the deck load.
 */
async function _freeQuietly(decoder: StemFlacDecoder): Promise<void> {
	try {
		await decoder.free();
	} catch {
		// The worker is unreachable either way; the caller's error is the news.
	}
}

function _returnDecoder(decoder: StemFlacDecoder): void {
	if (_pool.length >= MAX_POOLED_DECODERS) {
		void decoder.free();
		return;
	}
	_pool.push(decoder);
}

async function _decodeOnePart(
	ctx: BaseAudioContext,
	bytes: ArrayBuffer,
	makeDecoder: StemFlacDecoderFactory | null,
	fallback: (bytes: ArrayBuffer) => Promise<AudioBuffer>
): Promise<{ buffer: AudioBuffer; refusal: StemDecodeRefusal | null }> {
	if (!isFlacContainer(bytes)) {
		return { buffer: await fallback(bytes), refusal: 'not-flac' };
	}
	if (makeDecoder === null) {
		return { buffer: await fallback(bytes), refusal: 'decoder-unavailable' };
	}
	let decoder: StemFlacDecoder | null = null;
	let refusal: StemDecodeRefusal;
	try {
		decoder = await _takeDecoder(makeDecoder);
		const result = await decoder.decodeFile(new Uint8Array(bytes));
		const built = stemAudioBuffer(ctx, result);
		if (typeof built !== 'string') {
			_returnDecoder(decoder);
			decoder = null;
			return { buffer: built, refusal: null };
		}
		// A refusal for the BUNDLE's shape (wrong rate, not FLAC) leaves a
		// healthy decoder, which is parked. A refusal the DECODER reported
		// does not: it has processed a damaged stream, so it goes the same
		// way a thrown decode does.
		if (built === 'decode-failed') await _freeQuietly(decoder);
		else _returnDecoder(decoder);
		decoder = null;
		refusal = built;
	} catch {
		// Not returned to the pool - see above.
		if (decoder !== null) await _freeQuietly(decoder);
		refusal = 'decode-failed';
	}
	// OUTSIDE the try on purpose: `decodeAudioData` detaches its input, so a
	// fallback called from inside would be retried by the catch on a detached
	// buffer, and that meaningless second failure would replace the real one.
	return { buffer: await fallback(bytes), refusal };
}

/**
 * The reports as ring labels: which path the bundle took, and why not.
 *
 * Reported as counts plus the distinct reasons rather than per part, because
 * a bundle is single-codec by contract and four identical rows would say the
 * same thing four times.
 */
export function stemDecodeLabels(reports: readonly StemPartDecodeReport[]): Record<string, string> {
	const viaWorker = reports.filter((report) => report.viaWorker).length;
	const refusals = [...new Set(reports.map((r) => r.refusal).filter((r) => r !== null))];
	const lane = _preferredLane;
	return {
		stem_decode: viaWorker === 0 ? 'main-thread' : viaWorker === reports.length ? 'workers' : 'mixed',
		stem_decode_workers: `${viaWorker}/${reports.length}`,
		...(lane === null ? {} : { stem_decode_lane: lane }),
		...(refusals.length === 0 ? {} : { stem_decode_refused: refusals.join('+') })
	};
}

/**
 * The module's session state, reachable for tests and for a live probe.
 *
 * ONE export rather than five named ones: the pool and the settled lane are
 * deliberately module-scoped (they are a page's worth of workers and a page's
 * worth of measurement), so every reader of them is a test or an instrument,
 * and five separate entry points would each read to the export ratchet as an
 * unused public API.
 */
export const stemDecodeSession = {
	/** The lane in force, or null while it has never been measured. */
	lane: (): DecodeLane | null => _preferredLane,
	/** How many decoders are parked right now. */
	pooled: (): number => _pool.length,
	/** Settle the lane without running the two calibration loads. */
	forceLane: (lane: DecodeLane): void => {
		_preferredLane = lane;
		_laneTrials.clear();
		_trialLane = null;
		_trialContended = false;
	},
	/** Forget the pool. The workers themselves are dropped, not freed. */
	resetPool: (): void => {
		_pool.length = 0;
	},
	/** Forget the verdict and both trials. */
	resetLane: (): void => {
		_preferredLane = null;
		_laneTrials.clear();
		// The reservation goes too, or a reset between two loads would leave a
		// trial claimed forever and every later load pinned to the main thread.
		_trialLane = null;
		_trialContended = false;
	},
	/** Whether a calibration load is running right now. For tests and probes. */
	trialing: (): boolean => _trialLane !== null,
	/** How many loads are decoding right now. The contention denominator. */
	loadsInFlight: (): number => _loadsInFlight
};
