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
 * 1. NOT FLAC -> `decodeAudioData`. v3 bundles may ship any one codec and
 *    this decoder is FLAC-only. Sniffed from the BYTES, never a manifest
 *    field, so it cannot disagree with what was actually served.
 * 2. WRONG SAMPLE RATE -> `decodeAudioData`. The subtle one. `decodeAudioData`
 *    RESAMPLES to the context rate and a WASM decoder does not, so a 48kHz
 *    stem through the fast path into a 44.1kHz context is right in SAMPLES and
 *    wrong in SECONDS. `validateStemBufferAlignment` compares the parts to
 *    EACH OTHER and passes all four; the engine's stem/source check catches it
 *    one step later and FAILS a load `decodeAudioData` would have handled.
 *    Read from the STREAMINFO header first (`flac-header.ts`) so an ineligible
 *    bundle never spends a worker decode it is going to discard.
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

import {
	freeQuietly,
	forgetPool,
	pooledCount,
	returnDecoder,
	takeDecoder,
	warmPool
} from './flac-decoder-pool';
import type {
	DecodedStemAudio,
	StemFlacDecoder,
	StemFlacDecoderFactory
} from './flac-decoder-pool';
import { flacStreamSampleRate, isFlacContainer } from './flac-header';

export type { DecodedStemAudio, StemFlacDecoder, StemFlacDecoderFactory };

/** Why a part did NOT take the worker path. `null` means it did. */
/**
 * Which lane a part took, and why.
 *
 * Three are not refusals at all. `calibrating` is one of the two trial loads
 * a session runs to find the faster lane on THIS engine; `engine-prefers-main`
 * is every load after that measurement said workers would be slower here;
 * `awaiting-calibration` is a load that ran with no verdict AND could not be a
 * trial itself - it overlapped another load, or its bundle was ineligible.
 * Distinct from `engine-prefers-main` because nothing was measured, and a ring
 * row claiming an engine preferred the main thread when no comparison ever ran
 * would be a verdict invented out of a scheduling accident.
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

export { isFlacContainer };

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
 * Parts are started together and awaited together: the whole point is that
 * four workers overlap, so a sequential loop would spend the dependency and
 * keep the baseline. A part that refuses falls back INDIVIDUALLY rather than
 * condemning the bundle - a v3 bundle is single-codec by server contract, so
 * this is all-or-nothing in practice, but a per-part rule cannot be wrong
 * about a bundle that is not.
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
	const claim = _claimLane(parts.every((part) => _headerRefusal(ctx, encoded[part]) === null));
	let claimReleased = false;
	try {
		// Resolved ONCE for the bundle: four concurrent dynamic imports of one
		// module are four chances to pay the resolve twice.
		let makeDecoder: StemFlacDecoderFactory | null = options.makeDecoder ?? null;
		if (makeDecoder === null && claim.lane === 'workers') {
			try {
				makeDecoder = await _defaultDecoderFactory();
			} catch {
				makeDecoder = null;
			}
		}

		// Warmed BEFORE the clock starts, and only for the load that measures
		// the worker lane. The trial is a stopwatch over steady-state
		// throughput, and the worker trial is the load that first builds the
		// pool, so spawn plus wasm compile would be charged to the one load
		// whose number decides the session. Measured on this repo's own live
		// run: the stopwatch had the workers 1.71x faster while the trial that
		// paid the cold start settled on the main thread, permanently.
		if (claim.trialing && claim.lane === 'workers' && makeDecoder !== null) {
			await warmPool(makeDecoder, parts.length);
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
				const barred = _headerRefusal(ctx, bytes);
				const outcome =
					claim.lane === 'main-thread'
						? {
								// Sniffed on this lane too. A part the bytes already bar
								// could never have taken the worker path, and reporting
								// the lane reason instead would hide a permanent cause
								// behind a per-session one.
								buffer: await fallback(bytes),
								refusal: barred ?? mainThreadRefusal
							}
						: await _decodeOnePart(ctx, bytes, makeDecoder, fallback, barred);
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

/**
 * What the BYTES already rule out, before any decoder is spent on them.
 *
 * The rate half is the one that matters: a bundle recorded at a rate this
 * context does not run can never take the worker path, and reading that AFTER
 * the decode meant decoding it twice - once in a worker, discarded, once
 * natively - on every load forever, since a refused load is never a clean
 * trial, so the session never settled and the next load tried again.
 *
 * A header that does not disclose its rate is ELIGIBLE, not barred: the
 * decoder's own verdict still catches it, and refusing on a guess would cost
 * the rung every stream this module cannot parse.
 */
function _headerRefusal(
	ctx: BaseAudioContext,
	bytes: ArrayBuffer | undefined
): StemDecodeRefusal | null {
	if (bytes === undefined || !isFlacContainer(bytes)) return 'not-flac';
	const rate = flacStreamSampleRate(bytes);
	return rate !== null && rate !== ctx.sampleRate ? 'sample-rate-mismatch' : null;
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
 * Measured (`pnpm test:live:stem-decode-workers`): WebKit 909ms main-thread vs
 * 556ms in workers, 1.63x FASTER; Chromium 266ms vs 462ms, 1.7x SLOWER.
 * Chromium's decoder does not have the limit this rung
 * routes around, so there the rung is a regression.
 *
 * Hard-coding either answer pins a VALUE that rots the next time an engine
 * ships a decoder change, and one already did. So the first two stemmed loads
 * of a session MEASURE: load one entirely on the main thread, load two
 * entirely in workers, each normalized to encoded bytes per wall millisecond,
 * and the winner holds the lane for the session.
 *
 * WHOLE loads, one lane each, never a single load split between them. The
 * split version was written first and was WRONG: a main-thread decode running
 * alongside three saturated workers is starved by them, so that lane measured
 * slow for a reason unrelated to the question and Chromium calibrated to the
 * lane 1.7x slower there. Caught by the live test's stopwatch cross-check.
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
 * 3. Only ELIGIBLE bundles are trialed. The worker lane can only ever trial
 *    FLAC at this context's rate; anything else refuses per part, so trialing
 *    it would settle a FLAC lane from another codec or another rate.
 *
 * A load that cannot be a trial runs the MAIN THREAD, which ships today,
 * rather than guessing at the unmeasured lane.
 */
function _claimLane(everyPartIsEligible: boolean): LaneClaim {
	_loadsInFlight += 1;
	if (_trialLane !== null) _trialContended = true;
	if (_preferredLane !== null) return { lane: _preferredLane, trialing: false };
	if (_trialLane !== null || _loadsInFlight > 1 || !everyPartIsEligible) {
		return { lane: 'main-thread', trialing: false };
	}
	// Main thread first: a session that loads one stemmed deck pays nothing.
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

async function _decodeOnePart(
	ctx: BaseAudioContext,
	bytes: ArrayBuffer,
	makeDecoder: StemFlacDecoderFactory | null,
	fallback: (bytes: ArrayBuffer) => Promise<AudioBuffer>,
	barred: StemDecodeRefusal | null
): Promise<{ buffer: AudioBuffer; refusal: StemDecodeRefusal | null }> {
	// No decoder for bytes already ruled out: that decode would be discarded.
	if (barred !== null) return { buffer: await fallback(bytes), refusal: barred };
	if (makeDecoder === null) {
		return { buffer: await fallback(bytes), refusal: 'decoder-unavailable' };
	}
	let decoder: StemFlacDecoder | null = null;
	let refusal: StemDecodeRefusal;
	try {
		decoder = await takeDecoder(makeDecoder);
		const result = await decoder.decodeFile(new Uint8Array(bytes));
		const built = stemAudioBuffer(ctx, result);
		if (typeof built !== 'string') {
			returnDecoder(decoder);
			decoder = null;
			return { buffer: built, refusal: null };
		}
		// A refusal for the BUNDLE's shape (wrong rate, not FLAC) leaves a
		// healthy decoder, which is parked. A refusal the DECODER reported
		// does not: it has processed a damaged stream, so it goes the same
		// way a thrown decode does.
		if (built === 'decode-failed') await freeQuietly(decoder);
		else returnDecoder(decoder);
		decoder = null;
		// Only reachable when the header did NOT disclose the rate, since a
		// disclosed mismatch never takes a decoder at all. This worker decode
		// is already wasted, and without settling here every later load wastes
		// one too: the refusal keeps the trial unclean, so nothing ever
		// settles and the workers are tried again on the next load, forever.
		if (built === 'sample-rate-mismatch') _preferredLane = 'main-thread';
		refusal = built;
	} catch {
		// Not returned to the pool - see above.
		if (decoder !== null) await freeQuietly(decoder);
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
 * Counts plus distinct reasons rather than one row per part: a bundle is
 * single-codec by contract, so four rows would say the same thing four times.
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
 * ONE export rather than seven named ones: the pool and the settled lane are
 * module-scoped on purpose (a page's worth of workers, a page's worth of
 * measurement), so every reader is a test or an instrument, and seven entry
 * points would each read to the export ratchet as unused public API.
 */
export const stemDecodeSession = {
	/** The lane in force, or null while it has never been measured. */
	lane: (): DecodeLane | null => _preferredLane,
	/** How many decoders are parked right now. */
	pooled: (): number => pooledCount(),
	/** Settle the lane without running the two calibration loads. */
	forceLane: (lane: DecodeLane): void => {
		_preferredLane = lane;
		_laneTrials.clear();
		_trialLane = null;
		_trialContended = false;
	},
	/** Forget the pool. The workers themselves are dropped, not freed. */
	resetPool: forgetPool,
	/** Forget the verdict and both trials, reservation included: a reset that
	 * left one claimed would pin every later load to the main thread. */
	resetLane: (): void => {
		_preferredLane = null;
		_laneTrials.clear();
		_trialLane = null;
		_trialContended = false;
	},
	/** Whether a calibration load is running right now. For tests and probes. */
	trialing: (): boolean => _trialLane !== null,
	/** How many loads are decoding right now. The contention denominator. */
	loadsInFlight: (): number => _loadsInFlight
};
