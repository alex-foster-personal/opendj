/**
 * Round load latency for the deck corner readout.
 *
 * Latency improvement role: display-only - keeps the number glanceable
 * (2.3s to load / ~300ms to load) so warm vs cold prefetch is obvious
 * without DevTools.
 *
 * The " to load" suffix is part of the readout, not decoration: the deck
 * corner sits next to several other durations, and a bare "~600ms" does not
 * say which one it is (pin 0da471a39765, the maintainer, Wed 2 Sep 2026). The
 * not-measured case stays the empty string so nothing is labelled at all.
 */
const SUFFIX = ' to load';

export function formatLoadLatency(ms: number): string {
	if (!Number.isFinite(ms) || ms < 0) return '';
	if (ms >= 1000) {
		const tenths = Math.round(ms / 100) / 10;
		return `${tenths.toFixed(1)}s${SUFFIX}`;
	}
	const rounded = Math.max(50, Math.round(ms / 50) * 50);
	return `~${rounded}ms${SUFFIX}`;
}

// ------------------------------------------------------------------ load KPI

const AUDIO_TRANSFER_INTERNAL = ['fetchAudio', 'fetchAudioCacheHit', 'decodedShareHit'] as const;
const DECODE_INTERNAL = ['decodeMix', 'decodeMixShared'] as const;

/** Canonical blocking-load stage keys written to deck KPI and perf rows (issue #11142). */
export const DECK_LOAD_BLOCKING_STAGE_KEYS = [
	'audioTransfer',
	'decode',
	'stretchProcessorCreate',
	'stretchProcessorLoad',
	'processorLatency',
	'total',
	'probeStem',
	'fetchStems',
	'decodeStems'
] as const;

export type DeckLoadBlockingStageKey = (typeof DECK_LOAD_BLOCKING_STAGE_KEYS)[number];

export interface DeckLoadBlockingStageSnapshot {
	/** Ms-only map keyed by canonical blocking-load stage names. */
	durations_ms: Record<string, number>;
	/** Internal `time()` name that produced each canonical duration. */
	stage_sources: Record<string, string>;
}

function _firstFiniteMs(
	raw: Readonly<Record<string, number>>,
	names: readonly string[]
): { ms: number; source: string } | null {
	for (const name of names) {
		const value = raw[name];
		if (typeof value === 'number' && Number.isFinite(value)) return { ms: value, source: name };
	}
	return null;
}

/** Build the first-class blocking-load KPI from a raw `load()` stage map. */
export function snapshotDeckLoadBlockingStages(
	raw: Readonly<Record<string, number>>
): DeckLoadBlockingStageSnapshot {
	const durations_ms: Record<string, number> = {};
	const stage_sources: Record<string, string> = {};

	const transfer = _firstFiniteMs(raw, AUDIO_TRANSFER_INTERNAL);
	if (transfer !== null) {
		durations_ms.audioTransfer = transfer.ms;
		stage_sources.audioTransfer = transfer.source;
	}
	const decode = _firstFiniteMs(raw, DECODE_INTERNAL);
	if (decode !== null) {
		durations_ms.decode = decode.ms;
		stage_sources.decode = decode.source;
	}
	for (const [canonical, internal] of [
		['stretchProcessorCreate', 'stretchCreate'],
		['stretchProcessorLoad', 'stretchLoad'],
		['processorLatency', 'processorLatency'],
		['probeStem', 'probeStem'],
		['fetchStems', 'fetchStems'],
		['decodeStems', 'decodeStems']
	] as const) {
		const value = raw[internal];
		if (typeof value === 'number' && Number.isFinite(value)) {
			durations_ms[canonical] = value;
			stage_sources[canonical] = internal;
		}
	}
	const total = raw.total ?? raw.failedAt;
	if (typeof total === 'number' && Number.isFinite(total)) {
		durations_ms.total = total;
		stage_sources.total = raw.total !== undefined ? 'total' : 'failedAt';
	}
	return { durations_ms, stage_sources };
}
