/**
 * Q18 rung 1+3: decode a stem bundle's FLAC or MPEG parts in real Web Workers.
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
 * 1. NOT FLAC/MPEG -> `decodeAudioData`. v3 bundles may ship any one codec and
 *    this decoder is FLAC/MPEG-only. Sniffed from the BYTES, never a manifest
 *    field, so it cannot disagree with what was actually served.
 * 2. WRONG SAMPLE RATE -> `decodeAudioData`. The subtle one. `decodeAudioData`
 *    RESAMPLES to the context rate and a WASM decoder does not, so a 48kHz
 *    stem through the fast path into a 44.1kHz context is right in SAMPLES and
 *    wrong in SECONDS. `validateStemBufferAlignment` compares the parts to
 *    EACH OTHER and passes all four; the engine's stem/source check catches it
 *    one step later and FAILS a load `decodeAudioData` would have handled.
 *    Read from the header first so an ineligible bundle never spends a worker
 *    decode it is going to discard.
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
import { isMpegContainer, mpegStreamSampleRate } from './mpeg-header';
import {
	claimLane,
	isMpegRungShipped,
	type LaneClaim,
	releaseLane,
	settleContextOnMainThread,
	settledLane,
	type StemDecodeCodec
} from './stem-decode-lane';
import { settleWithWidth, stemDecodeWidth } from './stem-decode-width';

export type { DecodedStemAudio, StemFlacDecoderFactory };
export { isFlacContainer, stemDecodeWidth };

/** Why a part did NOT take the worker path. `null` means it did. */
export type StemDecodeRefusal =
	| 'not-flac'
	| 'sample-rate-mismatch'
	| 'decoder-unavailable'
	| 'decode-failed'
	| 'empty-decode'
	| 'calibrating'
	| 'awaiting-calibration'
	| 'engine-prefers-main';

export interface StemPartDecodeReport {
	part: string;
	viaWorker: boolean;
	refusal: StemDecodeRefusal | null;
	ms: number;
	bytes: number;
	codec: StemDecodeCodec | null;
}

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
		const source = decoded.channelData[channel] as Float32Array<ArrayBuffer>;
		buffer.copyToChannel(
			source.length > decoded.samplesDecoded ? source.subarray(0, decoded.samplesDecoded) : source,
			channel
		);
	}
	return buffer;
}

async function _flacDecoderFactory(): Promise<StemFlacDecoderFactory> {
	const mod = await import('@wasm-audio-decoders/flac');
	return (): StemFlacDecoder => new mod.FLACDecoderWebWorker();
}

/*
 * No bundled MPEG decoder. `mpg123-decoder` (LGPL-2.1 libmpg123 in WASM) was
 * the bench decoder for PERF-STEMDEC-03 and never shipped: the rung lost on
 * Chromium and failed its LSB check there, so it was removed rather than
 * carried as an LGPL chunk in the bundle. An `mpeg` claim with no injected
 * `makeDecoder` takes the decodeAudioData fallback.
 * See research/mp3-decoder/2026-10-01-mp3-decoder-license.md.
 */

export interface StemDecodeOptions {
	makeDecoder?: StemFlacDecoderFactory;
	decodeFallback?: (bytes: ArrayBuffer) => Promise<AudioBuffer>;
	now?: () => number;
	/** PERF-STEMDEC-04: parts decoded at once, read before each part starts.
	 * Defaults to every part at once. */
	width?: () => number;
}

export interface StemDecodeResult<P extends string> {
	buffers: Record<P, AudioBuffer>;
	reports: StemPartDecodeReport[];
	/** The smallest decode width in force when any part started. */
	width: number;
}

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

	const width = options.width ?? ((): number => parts.length);
	const bundleCodec = _bundleCodec(encoded, parts);
	// A narrowed load is never a lane trial: its wall time measures the width,
	// not the lane, and would settle the verdict on the wrong number.
	const claim = claimLane(
		parts.length,
		width() >= parts.length &&
			bundleCodec !== null &&
			parts.every((part) => _headerRefusal(ctx, encoded[part], bundleCodec) === null),
		bundleCodec ?? 'flac'
	);
	const poolKey = claim.codec;
	let claimReleased = false;
	try {
		let makeDecoder: StemFlacDecoderFactory | null = options.makeDecoder ?? null;
		if (makeDecoder === null && claim.lane === 'workers') {
			try {
				makeDecoder = claim.codec === 'mpeg' ? null : await _flacDecoderFactory();
			} catch {
				makeDecoder = null;
			}
		}

		if (claim.trialing && claim.lane === 'workers' && makeDecoder !== null) {
			await warmPool(makeDecoder, parts.length, poolKey);
		}
		const loadStarted = now();
		const mainThreadRefusal = _mainThreadRefusal(claim);
		const reports: StemPartDecodeReport[] = [];
		let narrowed = false;
		const { settled, narrowest } = await settleWithWidth(
			parts,
			() => {
				const limit = width();
				if (limit < parts.length) narrowed = true;
				return limit;
			},
			async (part) => {
				const started = now();
				const bytes = encoded[part] as ArrayBuffer;
				const byteLength = bytes === undefined ? 0 : bytes.byteLength;
				const barred = _headerRefusal(ctx, bytes, bundleCodec);
				const outcome =
					claim.lane === 'main-thread'
						? {
								buffer: await fallback(bytes),
								refusal: barred ?? mainThreadRefusal
							}
						: await _decodeOnePart(ctx, bytes, makeDecoder, fallback, barred, poolKey);
				reports.push({
					part,
					viaWorker: outcome.refusal === null,
					refusal: outcome.refusal,
					ms: Math.round(now() - started),
					bytes: byteLength,
					codec: _partCodec(bytes)
				});
				return [part, outcome.buffer] as const;
			}
		);
		const failure = settled.find((result) => result.status === 'rejected');
		if (failure !== undefined) throw (failure as PromiseRejectedResult).reason;
		const decoded = settled.map(
			(result) => (result as PromiseFulfilledResult<readonly [P, AudioBuffer]>).value
		);
		reports.sort((a, b) => parts.indexOf(a.part as P) - parts.indexOf(b.part as P));
		const clean =
			claim.lane === 'main-thread'
				? reports.every((r) => r.refusal === 'calibrating')
				: reports.every((r) => r.refusal === null);
		// A deck that started playing mid-trial narrowed the tail: not a measurement.
		releaseLane(
			claim,
			clean && !narrowed,
			reports.reduce((sum, r) => sum + r.bytes, 0),
			now() - loadStarted
		);
		claimReleased = true;
		return {
			buffers: Object.fromEntries(decoded) as Record<P, AudioBuffer>,
			reports,
			width: Math.min(narrowest, parts.length)
		};
	} finally {
		if (!claimReleased) releaseLane(claim, false, 0, 0);
	}
}

function _partCodec(bytes: ArrayBuffer | undefined): StemDecodeCodec | null {
	if (bytes === undefined) return null;
	if (isFlacContainer(bytes)) return 'flac';
	if (isMpegRungShipped() && isMpegContainer(bytes)) return 'mpeg';
	return null;
}

function _bundleCodec<P extends string>(
	encoded: Partial<Record<P, ArrayBuffer>>,
	parts: readonly P[]
): StemDecodeCodec | null {
	let codec: StemDecodeCodec | null = null;
	for (const part of parts) {
		const partCodec = _partCodec(encoded[part]);
		if (partCodec === null) return null;
		if (codec === null) codec = partCodec;
		else if (codec !== partCodec) return null;
	}
	return codec;
}

function _headerRefusal(
	ctx: BaseAudioContext,
	bytes: ArrayBuffer | undefined,
	bundleCodec: StemDecodeCodec | null
): StemDecodeRefusal | null {
	if (bytes === undefined) return 'not-flac';
	const codec = _partCodec(bytes);
	if (codec === null) return 'not-flac';
	if (bundleCodec !== null && codec !== bundleCodec) return 'not-flac';
	const rate =
		codec === 'flac' ? flacStreamSampleRate(bytes) : mpegStreamSampleRate(bytes);
	return rate !== null && rate !== ctx.sampleRate ? 'sample-rate-mismatch' : null;
}

function _mainThreadRefusal(claim: LaneClaim): StemDecodeRefusal {
	if (claim.trialing) return 'calibrating';
	return settledLane(claim.width, claim.codec) === null
		? 'awaiting-calibration'
		: 'engine-prefers-main';
}

async function _decodeOnePart(
	ctx: BaseAudioContext,
	bytes: ArrayBuffer,
	makeDecoder: StemFlacDecoderFactory | null,
	fallback: (bytes: ArrayBuffer) => Promise<AudioBuffer>,
	barred: StemDecodeRefusal | null,
	poolKey: string
): Promise<{ buffer: AudioBuffer; refusal: StemDecodeRefusal | null }> {
	if (barred !== null) return { buffer: await fallback(bytes), refusal: barred };
	if (makeDecoder === null) {
		return { buffer: await fallback(bytes), refusal: 'decoder-unavailable' };
	}
	let decoder: StemFlacDecoder | null = null;
	let refusal: StemDecodeRefusal;
	try {
		decoder = await takeDecoder(makeDecoder, poolKey);
		const result = await decoder.decodeFile(new Uint8Array(bytes));
		const built = stemAudioBuffer(ctx, result);
		if (typeof built !== 'string') {
			returnDecoder(decoder, poolKey);
			decoder = null;
			return { buffer: built, refusal: null };
		}
		if (built === 'decode-failed') await freeQuietly(decoder);
		else returnDecoder(decoder, poolKey);
		decoder = null;
		if (built === 'sample-rate-mismatch') settleContextOnMainThread();
		refusal = built;
	} catch {
		if (decoder !== null) await freeQuietly(decoder);
		refusal = 'decode-failed';
	}
	return { buffer: await fallback(bytes), refusal };
}

export function stemDecodeLabels(reports: readonly StemPartDecodeReport[]): Record<string, string> {
	const viaWorker = reports.filter((report) => report.viaWorker).length;
	const refusals = [...new Set(reports.map((r) => r.refusal).filter((r) => r !== null))];
	const codec = reports.find((r) => r.codec !== null)?.codec ?? null;
	const lane = settledLane(reports.length, codec ?? 'flac');
	return {
		stem_decode: viaWorker === 0 ? 'main-thread' : viaWorker === reports.length ? 'workers' : 'mixed',
		stem_decode_workers: `${viaWorker}/${reports.length}`,
		...(lane === null ? {} : { stem_decode_lane: lane }),
		...(codec === 'mpeg' ? { stem_decode_codec: 'mpeg' } : {}),
		...(refusals.length === 0 ? {} : { stem_decode_refused: refusals.join('+') })
	};
}

