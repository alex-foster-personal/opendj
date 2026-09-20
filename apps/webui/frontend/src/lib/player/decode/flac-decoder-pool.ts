/**
 * The page's stem worker decoders, checked out and put back.
 *
 * Decoders live for the PAGE, not for the load. Bounded by the widest layout:
 * four workers total per codec pool, never four per deck. FLAC and MPEG workers
 * cannot share a pool: reset of a FLAC wasm heap is not an MPEG decoder.
 */

/** One stem decoder running in its own worker. */
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
 */
export interface DecodedStemAudio {
	channelData: readonly Float32Array[];
	samplesDecoded: number;
	sampleRate: number;
	errors?: readonly unknown[];
}

const MAX_POOLED_DECODERS = 4;
const _pools = new Map<string, StemFlacDecoder[]>();
const _liveDecoders = new Set<StemFlacDecoder>();

function _trackDecoder(decoder: StemFlacDecoder): void {
	_liveDecoders.add(decoder);
}

async function _retireDecoder(decoder: StemFlacDecoder): Promise<void> {
	_liveDecoders.delete(decoder);
	await freeQuietly(decoder);
}

function _pool(poolKey: string): StemFlacDecoder[] {
	let pool = _pools.get(poolKey);
	if (pool === undefined) {
		pool = [];
		_pools.set(poolKey, pool);
	}
	return pool;
}

/** How many decoders are parked right now for `poolKey`. */
export function pooledCount(poolKey = 'flac'): number {
	return _pool(poolKey).length;
}

/** Forget the pool for `poolKey`, or every pool when omitted. */
export function forgetPool(poolKey?: string): void {
	if (poolKey === undefined) {
		_pools.clear();
		return;
	}
	_pool(poolKey).length = 0;
}

/** Live stem decoder workers (pooled or checked out). */
export function activeStemWorkerCount(): number {
	return _liveDecoders.size;
}

/** Terminate every pooled or checked-out decoder worker. */
export async function disposeStemDecoderPools(): Promise<void> {
	const failures: unknown[] = [];
	for (const decoder of [..._liveDecoders]) {
		try {
			await _retireDecoder(decoder);
		} catch (error) {
			failures.push(error);
		}
	}
	_pools.clear();
	if (failures.length === 1) throw failures[0];
	if (failures.length > 1) {
		throw new AggregateError(failures, 'disposeStemDecoderPools failed for multiple workers');
	}
}

export async function takeDecoder(
	make: StemFlacDecoderFactory,
	poolKey = 'flac'
): Promise<StemFlacDecoder> {
	const pool = _pool(poolKey);
	const pooled = pool.pop();
	if (pooled !== undefined) {
		try {
			await pooled.reset();
		} catch (exc) {
			await _retireDecoder(pooled);
			throw exc;
		}
		_trackDecoder(pooled);
		return pooled;
	}
	const fresh = make();
	try {
		await fresh.ready;
	} catch (exc) {
		await _retireDecoder(fresh);
		throw exc;
	}
	_trackDecoder(fresh);
	return fresh;
}

export function returnDecoder(decoder: StemFlacDecoder, poolKey = 'flac'): void {
	const pool = _pool(poolKey);
	if (pool.length >= MAX_POOLED_DECODERS) {
		void _retireDecoder(decoder);
		return;
	}
	pool.push(decoder);
}

export async function freeQuietly(decoder: StemFlacDecoder): Promise<void> {
	try {
		await decoder.free();
	} catch {
		// The worker is unreachable either way; the caller's error is the news.
	}
}

export async function warmPool(
	make: StemFlacDecoderFactory,
	count: number,
	poolKey = 'flac'
): Promise<void> {
	const pool = _pool(poolKey);
	const taken: StemFlacDecoder[] = [];
	try {
		while (taken.length < count && pool.length + taken.length < MAX_POOLED_DECODERS) {
			taken.push(await takeDecoder(make, poolKey));
		}
	} catch {
		// The load that follows calls takeDecoder again and reports per-part.
	} finally {
		for (const decoder of taken) returnDecoder(decoder, poolKey);
	}
}
