/**
 * TopBar memory meter model (PERF-R5 Q10) - pure, so the thresholds and the
 * hover copy are testable without a DOM.
 *
 * THE BUG THIS FIXES. `PerfMeters._updateMemory` read
 * `performance.memory.usedJSHeapSize` behind an `'memory' in performance`
 * check whose else-branch was `0`. `performance.memory` is a Chromium-only
 * API: in the shipping Tauri/WKWebView shell the check is false, so the heap
 * term silently became zero and the hover rendered "JS heap: 0 MB" as if it
 * had been measured. Two dishonest things followed from that. The number next
 * to the meter looked like heap + PCM while being PCM alone, and the
 * warn > 512 / crit > 1024 pair - calibrated against heap + PCM - was left
 * sitting on the smaller measurement, where a single stemmed deck nearly trips
 * warn on its own.
 *
 * The fix is not to invent a heap figure. It is to say which measurement is in
 * force (a marker on the number, a line in the hover) and to carry a threshold
 * pair per measurement.
 */

// ------------------------------------------------------------- thresholds

/**
 * Decoded PCM held by ONE stems-ready deck, in MiB.
 *
 * A 4-minute track at 44.1 kHz stereo Float32 is
 * 240 s x 44100 x 2 ch x 4 B = ~81 MiB of mix PCM. A stems-ready deck retains
 * the mix plus its 4 stem buffers, so 5x that: ~404 MiB. The engine tops out
 * at 4 decks, i.e. ~1616 MiB of decoded PCM at its designed ceiling.
 */
export const PCM_MB_PER_STEMMED_DECK = 404;

/** Heap + PCM pair, unchanged - calibrated on Chromium where both are real. */
export const HEAP_PLUS_PCM_WARN_MB = 512;
export const HEAP_PLUS_PCM_CRIT_MB = 1024;

/**
 * PCM-only pair, re-derived from the 4-stemmed-deck arithmetic above rather
 * than inherited from the heap + PCM pair.
 *
 * warn > 800 MiB: just under two of four decks stemmed (2 x 404 = 808). One
 * stemmed deck is an ordinary working state and must not warn; two is the
 * point where the remaining headroom is worth flagging.
 * crit > 1600 MiB: just under all four stemmed (4 x 404 = 1616), i.e. the
 * engine's designed ceiling. Anything past it is retention nothing accounts
 * for, which is exactly the GC-pressure state that causes audible skips.
 */
export const PCM_ONLY_WARN_MB = 800;
export const PCM_ONLY_CRIT_MB = 1600;

// ----------------------------------------------------------------- model

/** One 2-second sample of what the app is holding. */
export interface MemorySample {
	/** usedJSHeapSize in MiB, or null when this webview cannot measure it.
	 * null is a REAL state - never substitute 0. */
	jsHeapMB: number | null;
	pcmMB: number;
	anlzMB: number;
	anlzCount: number;
	prefetchMB: number;
	prefetchCount: number;
}

export interface MemoryReadout {
	totalMB: number;
	level: 'ok' | 'warn' | 'crit';
	/** Exactly what the meter renders, marker included. */
	text: string;
	hover: string;
	/** False when the figure is decoded PCM only. */
	heapMeasured: boolean;
}

/** Marker appended to a figure that is missing its heap term. */
const PCM_ONLY_MARKER = '*';

/**
 * True when this webview exposes the Chromium-only `performance.memory`.
 *
 * Call ONCE at init and keep the answer: the API cannot appear part way
 * through a session, and re-probing it every 2 s only invites the caller to
 * treat a missing reading as a transient zero.
 */
export function hasJsHeapApi(perf: unknown): boolean {
	return typeof perf === 'object' && perf !== null && 'memory' in perf;
}

/**
 * usedJSHeapSize in MiB, or null when there is no reading to be had.
 *
 * Null covers all three no-reading cases - no `performance`, no
 * `performance.memory` (WKWebView), and a `memory` object without the field -
 * because every one of them must render as "unavailable", not as 0 MB.
 */
export function readJsHeapMB(perf: unknown): number | null {
	if (!hasJsHeapApi(perf)) return null;
	const used = (perf as { memory?: { usedJSHeapSize?: number } }).memory?.usedJSHeapSize;
	if (typeof used !== 'number' || !Number.isFinite(used)) return null;
	return Math.round(used / (1024 * 1024));
}

/**
 * Turn one sample into the number, the level and the hover.
 *
 * ANLZ and prefetch bytes live INSIDE the JS heap, so they are never added on
 * top of it - they are breakdown lines only. When the heap cannot be measured
 * they fall outside the total entirely, and the hover says so rather than
 * leaving the reader to assume they are counted.
 */
export function memoryReadout(sample: MemorySample): MemoryReadout {
	const heapMeasured = sample.jsHeapMB !== null;
	const heapMB = sample.jsHeapMB ?? 0;
	const totalMB = heapMB + sample.pcmMB;
	const warnMB = heapMeasured ? HEAP_PLUS_PCM_WARN_MB : PCM_ONLY_WARN_MB;
	const critMB = heapMeasured ? HEAP_PLUS_PCM_CRIT_MB : PCM_ONLY_CRIT_MB;
	const level: MemoryReadout['level'] =
		totalMB > critMB ? 'crit' : totalMB > warnMB ? 'warn' : 'ok';

	const basis = heapMeasured ? 'JS heap + decoded PCM' : 'decoded PCM only';
	const heapLine = heapMeasured
		? `• JS heap: ${heapMB} MB`
		: `${PCM_ONLY_MARKER} JS heap: unavailable on this webview ` +
			'(performance.memory is a Chromium-only API); the figure above is decoded PCM only';
	const outsideTotal = heapMeasured ? '' : ' - NOT in the figure above';
	const thresholdLine = heapMeasured
		? `Thresholds (heap + PCM): warn > ${warnMB} MB, crit > ${critMB} MB`
		: `Thresholds (PCM only): warn > ${warnMB} MB, crit > ${critMB} MB - ` +
			`~${PCM_MB_PER_STEMMED_DECK} MB per stemmed 4-min deck, so warn is ` +
			'2 of 4 decks stemmed and crit is all 4';

	return {
		totalMB,
		level,
		text: `${totalMB}M${heapMeasured ? '' : PCM_ONLY_MARKER}`,
		heapMeasured,
		hover:
			`Approx retained: ${totalMB} MB (${basis})\n` +
			`${heapLine}\n` +
			`• ANLZ cache: ~${sample.anlzMB} MB (${sample.anlzCount} tracks, UNCAPPED, in heap${outsideTotal})\n` +
			`• Deck PCM (+ 4 stems when ready): ${sample.pcmMB} MB\n` +
			`• Audio prefetch: ${sample.prefetchMB} MB (${sample.prefetchCount} tracks, in heap${outsideTotal})\n` +
			thresholdLine
	};
}
