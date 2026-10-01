/**
 * PERF-STEMDEC-04: how many stem parts decode at once.
 *
 * Decoding a bundle's four parts together is the fastest way to stems on an
 * idle deck, and the wrong shape while another deck is audible. Measured Thu
 * 1 Oct 2026 on a loaded host (ops/perf/stem-decode-under-playback-round-0):
 * with deck 1 playing, the four-wide burst made the audio callback arrive
 * late at about 0.90 per second of decode against a 0.10 per second
 * background. Per bundle, pooled over 36 interleaved decodes: 0.47 late
 * callbacks four wide, 0.13 two wide, 0.14 one wide, the last two at the
 * background level. The render thread itself sat near 5% load throughout, so
 * the lateness is the decode threads taking the cores the audio thread
 * needed, not audio work. Two wide buys everything one wide does and costs
 * about 200 ms of decode against about 570 (round 2), so two is the width.
 *
 * Pure: the caller says whether a deck is playing.
 */

/** Parts decoded at once while any deck is audible. */
export const STEM_DECODE_WIDTH_WHILE_PLAYING = 2;

/** The width for a bundle of `partCount` parts: all of them at once on a
 * silent rig, {@link STEM_DECODE_WIDTH_WHILE_PLAYING} while a deck plays. */
export function stemDecodeWidth(partCount: number, playing: boolean): number {
	_assertWidth(partCount, 'partCount');
	return playing ? Math.min(partCount, STEM_DECODE_WIDTH_WHILE_PLAYING) : partCount;
}

function _assertWidth(value: number, name: string): void {
	if (!Number.isInteger(value) || value < 1) {
		throw new RangeError(`${name} must be a positive integer, got ${String(value)}`);
	}
}

export interface WidthSettled<R> {
	/** One result per item, in item order (the `Promise.allSettled` shape). */
	settled: PromiseSettledResult<R>[];
	/** The smallest width in force when any item started. */
	narrowest: number;
}

/**
 * `Promise.allSettled` with a ceiling on how many items run at once.
 *
 * `width` is read again before every start, so a deck that starts playing
 * halfway through a bundle narrows the parts not yet started, and one that
 * stops widens them. Every item runs even when an earlier one rejects.
 */
export function settleWithWidth<T, R>(
	items: readonly T[],
	width: () => number,
	run: (item: T) => Promise<R>
): Promise<WidthSettled<R>> {
	return new Promise((resolve, reject) => {
		const settled = new Array<PromiseSettledResult<R>>(items.length);
		let next = 0;
		let running = 0;
		let finished = 0;
		let narrowest = Number.POSITIVE_INFINITY;
		let failed = false;

		const pump = (): void => {
			if (failed) return;
			if (finished === items.length) {
				resolve({ settled, narrowest: Number.isFinite(narrowest) ? narrowest : 0 });
				return;
			}
			while (next < items.length) {
				let limit: number;
				try {
					limit = width();
					_assertWidth(limit, 'width');
				} catch (error) {
					failed = true;
					reject(error);
					return;
				}
				if (running >= limit) return;
				narrowest = Math.min(narrowest, limit);
				const index = next;
				next += 1;
				running += 1;
				const done = (result: PromiseSettledResult<R>): void => {
					settled[index] = result;
					running -= 1;
					finished += 1;
					pump();
				};
				let started: Promise<R>;
				try {
					started = run(items[index] as T);
				} catch (reason) {
					started = Promise.reject(reason);
				}
				started.then(
					(value) => done({ status: 'fulfilled', value }),
					(reason: unknown) => done({ status: 'rejected', reason })
				);
			}
		};
		pump();
	});
}
