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
 * Two of these are not refusals at all. `calibrating` is one of the two trial
 * loads a session runs to find out which lane is faster on THIS engine;
 * `engine-prefers-main` is every load after that measurement said the workers
 * would be slower here. See `_laneForThisLoad`.
 */
export type StemDecodeRefusal =
	| 'not-flac'
	| 'sample-rate-mismatch'
	| 'decoder-unavailable'
	| 'decode-failed'
	| 'empty-decode'
	| 'calibrating'
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
 */
export function stemAudioBuffer(
	ctx: BaseAudioContext,
	decoded: DecodedStemAudio
): AudioBuffer | StemDecodeRefusal {
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

	// Resolved ONCE for the bundle, not once per part: four concurrent dynamic
	// imports of the same module are four chances to pay the resolve twice.
	let makeDecoder: StemFlacDecoderFactory | null = options.makeDecoder ?? null;
	if (
		makeDecoder === null &&
		_laneForThisLoad() === 'workers' &&
		parts.some((part) => _flacBytes(encoded[part]))
	) {
		try {
			makeDecoder = await _defaultDecoderFactory();
		} catch {
			makeDecoder = null;
		}
	}

	const trialing = _preferredLane === null;
	const lane = _laneForThisLoad();
	const loadStarted = now();

	const reports: StemPartDecodeReport[] = [];
	const decoded = await Promise.all(
		parts.map(async (part) => {
			const started = now();
			const bytes = encoded[part] as ArrayBuffer;
			const byteLength = bytes === undefined ? 0 : bytes.byteLength;
			const outcome =
				lane === 'main-thread'
					? {
							buffer: await fallback(bytes),
							refusal: (trialing ? 'calibrating' : 'engine-prefers-main') as StemDecodeRefusal
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
	reports.sort((a, b) => parts.indexOf(a.part as P) - parts.indexOf(b.part as P));
	if (trialing) {
		// Only a CLEAN run of the lane counts. A load where some part refused
		// the worker path for its own reason did not measure that lane.
		const clean =
			lane === 'main-thread'
				? reports.every((r) => r.refusal === 'calibrating')
				: reports.every((r) => r.refusal === null);
		if (clean) {
			_recordTrial(
				lane,
				reports.reduce((sum, r) => sum + r.bytes, 0),
				now() - loadStarted
			);
		}
	}
	return { buffers: Object.fromEntries(decoded) as Record<P, AudioBuffer>, reports };
}

function _flacBytes(bytes: ArrayBuffer | undefined): boolean {
	return bytes !== undefined && isFlacContainer(bytes);
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

/** Which lane THIS load should run, whole. */
function _laneForThisLoad(): DecodeLane {
	if (_preferredLane !== null) return _preferredLane;
	// Main thread first: it is what ships today, so a session that only ever
	// loads one stemmed deck pays nothing for the trial.
	return _laneTrials.has('main-thread') ? 'workers' : 'main-thread';
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

async function _takeDecoder(make: StemFlacDecoderFactory): Promise<StemFlacDecoder> {
	const pooled = _pool.pop();
	if (pooled !== undefined) {
		await pooled.reset();
		return pooled;
	}
	const fresh = make();
	await fresh.ready;
	return fresh;
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
	try {
		decoder = await _takeDecoder(makeDecoder);
		const result = await decoder.decodeFile(new Uint8Array(bytes));
		const built = stemAudioBuffer(ctx, result);
		if (typeof built === 'string') {
			_returnDecoder(decoder);
			decoder = null;
			return { buffer: await fallback(bytes), refusal: built };
		}
		_returnDecoder(decoder);
		decoder = null;
		return { buffer: built, refusal: null };
	} catch {
		// Not returned to the pool - see above.
		if (decoder !== null) await decoder.free();
		decoder = null;
		return { buffer: await fallback(bytes), refusal: 'decode-failed' };
	}
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
	},
	/** Forget the pool. The workers themselves are dropped, not freed. */
	resetPool: (): void => {
		_pool.length = 0;
	},
	/** Forget the verdict and both trials. */
	resetLane: (): void => {
		_preferredLane = null;
		_laneTrials.clear();
	}
};
