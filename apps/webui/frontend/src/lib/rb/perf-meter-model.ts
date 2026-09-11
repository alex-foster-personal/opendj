/**
 * Pure model for PerfMeters v2 (PERFMODE-05, issue #1987).
 *
 * Interval math, chart history, sparkline geometry, and breakdown rows.
 * No DOM, no runes, no fetch - testable with loadTypeScriptModule.
 */

export const PRESSURE_SAMPLE_IDLE_MS = 1000;
export const PRESSURE_SAMPLE_ELEVATED_MS = 5000;
export const KERNEL_ELEVATED_LEVEL = 2;
export const PRESSURE_CHURN_EARLY_WARNING = 500;

/** One chart sample pushed on each monitor tick. */
export interface ChartSample {
	tMs: number;
	hz: number | null;
	cacheN: number;
	cacheMB: number;
	memoryMB: number;
	anlzMB: number;
	prefetchMB: number;
	swapMB: number | null;
	kernelLevel: number | null;
	churnScore: number | null;
}

export interface ProcessFamilyMember {
	label: string;
	mb: number;
}

export interface ProcessFamilySnapshot {
	kernelLevel: number | null;
	churnScore: number | null;
	compressorRate: number | null;
	members: ProcessFamilyMember[];
}

export type BreakdownReset = 'prefetch' | 'anlz' | 'perf-ring';

export interface BreakdownLine {
	key: string;
	label: string;
	value: string;
	title: string;
	reset?: BreakdownReset;
}

export interface SparkSegment {
	points: string;
	dashed: boolean;
}

export interface SparkGeometry {
	segments: SparkSegment[];
	bounds: { lo: number; hi: number };
}

export function isElevated(kernelLevel: number | null, churnScore: number | null): boolean {
	if (kernelLevel !== null && kernelLevel >= KERNEL_ELEVATED_LEVEL) return true;
	if (churnScore !== null && churnScore >= PRESSURE_CHURN_EARLY_WARNING) return true;
	return false;
}

export function perfMeterSampleIntervalMs(input: {
	kernelLevel: number | null;
	churnScore: number | null;
}): number {
	return isElevated(input.kernelLevel, input.churnScore)
		? PRESSURE_SAMPLE_ELEVATED_MS
		: PRESSURE_SAMPLE_IDLE_MS;
}

export function pushSample<T>(ring: T[], point: T, cap: number): T[] {
	const next = [...ring, point];
	if (next.length <= cap) return next;
	return next.slice(next.length - cap);
}

const SPARK_W = 120;
const SPARK_H = 32;
const SPARK_PAD = 4;

/**
 * Turn values into SVG polyline segments. null is a gap, never plotted as 0.
 */
export function sparkPoints(values: Array<number | null>): SparkGeometry {
	const present: Array<{ index: number; value: number }> = [];
	values.forEach((value, index) => {
		if (typeof value === 'number' && Number.isFinite(value)) {
			present.push({ index, value });
		}
	});

	const nums = present.map((point) => point.value);
	let lo = nums.length > 0 ? Math.min(...nums) : 0;
	let hi = nums.length > 0 ? Math.max(...nums) : 0;
	if (lo === hi) {
		lo -= 1;
		hi += 1;
	}

	const xAt = (index: number): number =>
		values.length <= 1
			? SPARK_W / 2
			: SPARK_PAD + ((SPARK_W - 2 * SPARK_PAD) * index) / (values.length - 1);
	const yAt = (value: number): number =>
		SPARK_H - SPARK_PAD - (SPARK_H - 2 * SPARK_PAD) * ((value - lo) / (hi - lo));

	const segments: SparkSegment[] = [];
	for (let i = 0; i < present.length - 1; i += 1) {
		const from = present[i];
		const to = present[i + 1];
		segments.push({
			points: `${xAt(from.index)},${yAt(from.value)} ${xAt(to.index)},${yAt(to.value)}`,
			dashed: to.index - from.index > 1
		});
	}

	return { segments, bounds: { lo, hi } };
}

export interface BreakdownInput {
	hz: number | null;
	hzQualityOk: boolean;
	hzTickHz: number | null;
	prefetchCount: number;
	prefetchMB: number;
	anlzCount: number;
	anlzMB: number;
	pcmMB: number;
	jsHeapMB: number | null;
	ringCount: number;
	swapMB: number | null;
	kernelLevel: number | null;
	churnScore: number | null;
	compressorRate: number | null;
	processes: ProcessFamilySnapshot | null;
}

/** Rows for the open monitor panel. null-valued signals are omitted entirely. */
export function breakdownLines(input: BreakdownInput): BreakdownLine[] {
	const lines: BreakdownLine[] = [];

	const hzTitle =
		!input.hzQualityOk && input.hz !== null && input.hzTickHz !== null
			? `Hz meter drift: displayed ${input.hz} vs presentation tick ${input.hzTickHz.toFixed(1)}. ` +
				'Audio presentation publish rate (Hz) while a deck is audible. Orange <45, red <30.'
			: 'Audio presentation publish rate (Hz) while a deck is audible. Orange <45, red <30.';
	lines.push({
		key: 'hz',
		label: 'Hz',
		value: input.hz === null ? '--' : String(input.hz),
		title: hzTitle
	});

	lines.push({
		key: 'prefetch',
		label: 'Audio prefetch',
		value: `${input.prefetchCount} tracks, ${input.prefetchMB} MB`,
		title: 'Row-select audio ArrayBuffers held for browse-ahead.',
		reset: 'prefetch'
	});

	lines.push({
		key: 'anlz',
		label: 'ANLZ cache',
		value: `${input.anlzCount} tracks, ${input.anlzMB} MB`,
		title: 'Waveform analysis cache entries (in JS heap).',
		reset: 'anlz'
	});

	lines.push({
		key: 'pcm',
		label: 'Deck PCM',
		value: `${input.pcmMB} MB`,
		title: 'Decoded PCM held by loaded decks (not resettable).'
	});

	if (input.jsHeapMB !== null) {
		lines.push({
			key: 'heap',
			label: 'JS heap',
			value: `${input.jsHeapMB} MB`,
			title: 'Chromium performance.memory.usedJSHeapSize.'
		});
	} else {
		lines.push({
			key: 'heap',
			label: 'JS heap',
			value: 'unavailable',
			title:
				'performance.memory is unavailable on this webview; ' +
				'the compact meter shows decoded PCM only.'
		});
	}

	lines.push({
		key: 'perf-ring',
		label: 'Perf ring',
		value: `${input.ringCount} rows`,
		title: 'Durable client-side performance event ring.',
		reset: 'perf-ring'
	});

	if (input.swapMB !== null) {
		lines.push({
			key: 'swap',
			label: 'Swap',
			value: `${input.swapMB} MB`,
			title: 'Swap space in use (machine pressure snapshot).'
		});
	}

	if (input.kernelLevel !== null) {
		lines.push({
			key: 'kernel-pressure',
			label: 'Kernel pressure',
			value: String(input.kernelLevel),
			title: 'kern.memorystatus_vm_pressure_level: 1 normal, 2 warning, 4 critical.'
		});
	}

	if (input.compressorRate !== null) {
		lines.push({
			key: 'compressor',
			label: 'Compressor',
			value: String(input.compressorRate),
			title: 'Memory compressor rate from the process probe.'
		});
	}

	if (input.churnScore !== null) {
		lines.push({
			key: 'churn',
			label: 'Churn score',
			value: String(input.churnScore),
			title: 'swap_rate * 10 + decomp_rate early-warning trend.'
		});
	}

	if (input.processes !== null) {
		for (const member of input.processes.members) {
			lines.push({
				key: `proc-${member.label}`,
				label: member.label,
				value: `${member.mb} MB`,
				title: 'Process-family footprint by role.'
			});
		}
	}

	return lines;
}
