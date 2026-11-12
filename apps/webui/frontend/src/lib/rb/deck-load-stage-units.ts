/**
 * Deck-load perf stage units: ms durations vs numeric facts.
 *
 * Pure helpers with no imports, so unit tests and aggregators share one contract
 * with `NON_DURATION_STAGES` in scripts/perf/perf_log_model.py.
 */

/** Non-duration keys that must never be summed or averaged as milliseconds. */
export const DECK_LOAD_STAGE_FACT_KEYS = [
	'audioBytes',
	'audioPrefetchHit',
	'stemmed',
	'deferredToStop',
	'decodedShareHit'
] as const;

export type DeckLoadStageFactKey = (typeof DECK_LOAD_STAGE_FACT_KEYS)[number];

/** Millisecond stage durations for one deck-load timing row. */
export type DeckLoadStagesMs = Record<string, number>;

/** Byte counts and 0/1 flags carried beside the ms map. */
export type DeckLoadStageFacts = Partial<Record<DeckLoadStageFactKey, number>>;

const _factKeySet = new Set<string>(DECK_LOAD_STAGE_FACT_KEYS);

export function isDeckLoadStageFactKey(name: string): name is DeckLoadStageFactKey {
	return _factKeySet.has(name);
}

export function splitDeckLoadStageMap(combined: Readonly<Record<string, number>>): {
	stagesMs: DeckLoadStagesMs;
	facts: DeckLoadStageFacts;
} {
	const stagesMs: DeckLoadStagesMs = {};
	const facts: DeckLoadStageFacts = {};
	for (const [key, value] of Object.entries(combined)) {
		if (!Number.isFinite(value)) continue;
		if (isDeckLoadStageFactKey(key)) {
			facts[key] = value;
		} else {
			stagesMs[key] = value;
		}
	}
	return { stagesMs, facts };
}

/**
 * Parse `[perf] deck-load ... stretchCreate=45 facts.audioBytes=5059178`.
 * Ms tokens land in `stagesMs`; `facts.*` tokens land in `facts`.
 */
export function parseDeckLoadPerfConsoleLine(text: string): {
	stagesMs: DeckLoadStagesMs;
	facts: DeckLoadStageFacts;
} | null {
	if (!text.startsWith('[perf] ')) return null;
	const stagesMs: DeckLoadStagesMs = {};
	const facts: DeckLoadStageFacts = {};
	for (const token of text.split(/\s+/)) {
		const eq = token.indexOf('=');
		if (eq <= 0) continue;
		const value = Number(token.slice(eq + 1));
		if (!Number.isFinite(value)) continue;
		const key = token.slice(0, eq);
		if (key.startsWith('facts.')) {
			const factKey = key.slice('facts.'.length);
			if (isDeckLoadStageFactKey(factKey)) {
				facts[factKey] = value;
			}
		} else {
			stagesMs[key] = value;
		}
	}
	return { stagesMs, facts };
}

export function formatDeckLoadPerfFacts(facts: Readonly<DeckLoadStageFacts>): string {
	return Object.entries(facts)
		.filter(([, v]) => Number.isFinite(v))
		.map(([k, v]) => `facts.${k}=${v}`)
		.join(' ');
}
