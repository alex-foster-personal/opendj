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
 * 240 s x 44100 x 2 ch x 4 B = ~81 MiB of mix PCM. A hold-neutral stemmed deck
 * retains mix JS, the mix worklet copy, and 4 stem parts, so 6x that: ~485 MiB.
 * The engine tops out at 4 decks, i.e. ~1940 MiB of decoded PCM at its designed ceiling.
 */
export const PCM_MB_PER_STEMMED_DECK = 485;

/** Heap + PCM pair, unchanged - calibrated on Chromium where both are real. */
export const HEAP_PLUS_PCM_WARN_MB = 512;
export const HEAP_PLUS_PCM_CRIT_MB = 1024;

/**
 * PCM-only pair, re-derived from the 4-stemmed-deck arithmetic above rather
 * than inherited from the heap + PCM pair.
 *
 * warn > 960 MiB: just under two of four decks stemmed (2 x 485 = 970). One
 * stemmed deck is an ordinary working state and must not warn; two is the
 * point where the remaining headroom is worth flagging.
 * crit > 1920 MiB: just under all four stemmed (4 x 485 = 1940), i.e. the
 * engine's designed ceiling. Anything past it is retention nothing accounts
 * for, which is exactly the GC-pressure state that causes audible skips.
 */
export const PCM_ONLY_WARN_MB = 960;
export const PCM_ONLY_CRIT_MB = 1920;

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
	/** Active ANLZ LRU cap; defaults to STANDARD when omitted. */
	anlzCap?: number;
	/** Active tier label for hover; defaults to STANDARD when omitted. */
	tierLabel?: string;
	/**
	 * The app's other processes as the engine measured them (resident MB):
	 * the engine, its sidecars (odj-audio, workers) and the desktop shell.
	 * null or omitted when the engine did not report them, which is a REAL
	 * state the hover names, never an empty family.
	 */
	processes?: AppProcess[] | null;
	/**
	 * Set when the engine had a native-probe sample but it was stale, so its
	 * footprints were left out of `processes`; the hover says so.
	 */
	probeStale?: { ageSeconds: number | null } | null;
}

/** One process of the app's family, as GET .../telemetry/processes reports it. */
export interface AppProcess {
	label: string;
	/** Resident MB, or null when the engine could not read it. */
	mb: number | null;
	/** The native probe's role, when the member came from it. */
	role?: string | null;
}

/**
 * Roles that ARE this page's own renderer: the Tauri shell's WebKit content
 * process (`webkit-webcontent`, from the native probe) and the Electron
 * shell's Chromium renderer (`electron-renderer`, from the engine's own walk,
 * set only for a `--type=renderer` child of the shell that stamped its pid
 * into the engine's environment). When one is measured the page's memory is
 * already inside the process total, so the in-page estimate is not added on
 * top of it. A plain browser tab is never in the family, so there the page
 * term is still added.
 */
const PAGE_PROCESS_ROLES: ReadonlySet<string> = new Set(['webkit-webcontent', 'electron-renderer']);

/**
 * The measured member that IS this page's own process, or null. Both the
 * compact figure and the pinned breakdown ask this one question, so they
 * cannot disagree about whether the heap is already counted.
 */
export function measuredPageProcess<T extends { mb: number | null; role?: string | null }>(
	members: readonly T[] | null | undefined
): T | null {
	return (
		members?.find((p) => p.mb !== null && p.role != null && PAGE_PROCESS_ROLES.has(p.role)) ?? null
	);
}

export interface MemoryReadout {
	/** What the meter shows: this page plus every measured app process. */
	totalMB: number;
	/** This page alone (JS heap + decoded PCM); the level is judged on it. */
	pageMB: number;
	/** The engine and sidecar processes, or null when they were not reported. */
	processesMB: number | null;
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
	const pageMB = heapMB + sample.pcmMB;
	// The thresholds were calibrated on what THIS page retains (GC pressure is
	// what skips audio), so the level stays judged on the page term even when
	// the figure also counts the engine's processes.
	const warnMB = heapMeasured ? HEAP_PLUS_PCM_WARN_MB : PCM_ONLY_WARN_MB;
	const critMB = heapMeasured ? HEAP_PLUS_PCM_CRIT_MB : PCM_ONLY_CRIT_MB;
	const level: MemoryReadout['level'] =
		pageMB > critMB ? 'crit' : pageMB > warnMB ? 'warn' : 'ok';
	const anlzCap = sample.anlzCap ?? 32;
	const tierLabel = sample.tierLabel ?? 'STANDARD';

	const processes = sample.processes ?? null;
	const measured = processes?.filter((p) => p.mb !== null) ?? [];
	const processesMB = processes === null ? null : measured.reduce((sum, p) => sum + (p.mb ?? 0), 0);
	const pageInProcesses = measuredPageProcess(processes) !== null;
	const totalMB = (processesMB ?? 0) + (pageInProcesses ? 0 : pageMB);
	// The marker flags a figure missing a term it should have: the heap, when
	// the page's own process was not measured either.
	const marked = !heapMeasured && !pageInProcesses;

	const basis = heapMeasured ? 'JS heap + decoded PCM' : 'decoded PCM only';
	// Three cases, because the figure holds the heap in two of them: read in
	// the page, or inside the page's own measured process (WKWebView, where the
	// page cannot read it but the content process footprint includes it).
	const heapLine = heapMeasured
		? pageInProcesses
			? `• JS heap: ${heapMB} MB (inside the measured page process listed below, not added again)`
			: `• JS heap: ${heapMB} MB`
		: pageInProcesses
			? '• JS heap: not readable on this webview (performance.memory is a Chromium-only ' +
				'API); it is inside the measured page process listed below'
			: `${PCM_ONLY_MARKER} JS heap: unavailable on this webview ` +
				'(performance.memory is a Chromium-only API); the figure above is decoded PCM only';
	const outsideTotal = heapMeasured || pageInProcesses ? '' : ' - NOT in the figure above';
	const thresholdLine = heapMeasured
		? `Thresholds (this page, heap + PCM): warn > ${warnMB} MB, crit > ${critMB} MB`
		: `Thresholds (this page, PCM only): warn > ${warnMB} MB, crit > ${critMB} MB - ` +
			`~${PCM_MB_PER_STEMMED_DECK} MB per stemmed 4-min deck, so warn is ` +
			'2 of 4 decks stemmed and crit is all 4';

	const pageLines =
		`${heapLine}\n` +
		`• ANLZ cache: ~${sample.anlzMB} MB (${sample.anlzCount}/${anlzCap} tracks, ${tierLabel}, in heap${outsideTotal})\n` +
		`• Deck PCM (mix JS + worklet copy; + 4 stems when ready or pending): ${sample.pcmMB} MB\n` +
		`• Audio prefetch: ${sample.prefetchMB} MB (${sample.prefetchCount} tracks, in heap${outsideTotal})\n`;

	// Said whether or not any process was reported: with no live walk the
	// stale sample is the only thing the engine could have counted.
	const staleLine = sample.probeStale
		? `Native probe sample is stale (${
				sample.probeStale.ageSeconds === null
					? 'age unknown'
					: sample.probeStale.ageSeconds < 0
						? `dated ${Math.round(-sample.probeStale.ageSeconds)} s in the future`
						: `${Math.round(sample.probeStale.ageSeconds)} s old`
			}) - its process footprints are left out of the figure above\n`
		: '';
	let head: string;
	let processLines: string;
	if (processes === null) {
		head = `Approx retained: ${totalMB} MB (this page, ${basis})\n`;
		processLines =
			'Engine and sidecar processes: unavailable (the engine did not report them) - NOT in the figure above\n' +
			staleLine;
	} else {
		const unmeasured = processes.length - measured.length;
		const pageTerm = pageInProcesses
			? 'this page measured in its own process'
			: `this page ~${pageMB} MB (${basis})`;
		head =
			`Approx resident: ${totalMB} MB (${pageTerm} + ` +
			`${processesMB} MB in ${measured.length} engine and sidecar processes)\n`;
		processLines =
			'Engine and sidecar processes (resident, measured by the engine):\n' +
			processes.map((p) => `• ${p.label}: ${p.mb === null ? 'unavailable' : `${p.mb} MB`}\n`).join('') +
			(unmeasured > 0 ? `${unmeasured} process(es) could not be read - NOT in the figure above\n` : '') +
			staleLine +
			(pageInProcesses
				? ''
				: "In a browser the tab's own footprint (Chrome's Task Manager) also holds DOM, " +
					'GPU and Web Audio buffers a page cannot read, so the page term is a floor\n');
	}

	return {
		totalMB,
		pageMB,
		processesMB,
		level,
		text: `${totalMB}M${marked ? PCM_ONLY_MARKER : ''}`,
		heapMeasured,
		hover: head + pageLines + processLines + thresholdLine
	};
}
