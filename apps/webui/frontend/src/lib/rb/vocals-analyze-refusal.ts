/**
 * Why VocalAnalyzeButton is inert for a row, or null when it can run.
 *
 * Modeled on track-drag-refusal.ts: one pure function so the button's
 * `disabled`/tooltip state is real, executed logic rather than a template
 * expression only readable by eye. `POST /vocals/analyze` mode "from-stems"
 * needs a `ready` local stem bundle (PARITY-08 / issue #1038); every other
 * `StemSummary` state is a refusal.
 */
import type { StemSummary, Vocals } from './api-rb';

/** True when `GET /vocals/{id}/status` already carries regions (issue #11422). */
export function vocalsStatusHasRegions(vocals: Vocals): boolean {
	return 'regions' in vocals && vocals.regions.length > 0;
}

export function vocalsAnalyzeRefusal(stems: StemSummary | null): string | null {
	if (stems === null || stems.status === 'none') {
		return 'vocal analysis needs a stem bundle - see PARITY-TODO';
	}
	if (stems.status === 'invalid') {
		return 'vocal analysis needs a stem bundle - see PARITY-TODO';
	}
	return null;
}

// ------------------------------------------------- server refusal copy
// Issue #9653 / UX-R2-03: the operator reads words, never a server category.
// The raw category stays on the button as data-agent-refusal for agents.

/** Every category `POST /vocals/analyze` can refuse with (apps/vocals/cli.py + routes/vocals.py). */
export const VOCALS_REFUSAL_COPY: Readonly<Record<string, string>> = {
	cached_demucs: 'Vocals already analyzed',
	missing_file: 'Audio file not found',
	missing_analysis: 'Track needs analysing first',
	pvdi: 'Vocals already come from rekordbox',
	broken_vocal_cache: 'Saved vocal data is unreadable',
	unknown_track: 'Track not found in the library',
	missing_stems: 'Stem bundle not found',
	in_progress: 'Vocal analysis is already running'
};

export const VOCALS_REFUSAL_GENERIC = 'Vocal analysis could not run for this track';

/** Operator-safe words for a server refusal category; unknown -> generic + console.error. */
export function vocalsRefusalCopy(category: string | null | undefined): string {
	if (typeof category === 'string' && Object.hasOwn(VOCALS_REFUSAL_COPY, category)) {
		return VOCALS_REFUSAL_COPY[category];
	}
	console.error('vocalsRefusalCopy: unknown vocal refusal category', category);
	return VOCALS_REFUSAL_GENERIC;
}

export interface VocalsAnalyzeOutcome {
	message: string;
	toastKind: 'info' | 'error';
	refresh: boolean;
	category: string | null;
}

/** Toast + refresh decision for one clicked row from the analyze response. */
export function vocalsAnalyzeOutcome(
	stableId: string,
	result: { claimed: readonly string[]; refused: Readonly<Record<string, string>> }
): VocalsAnalyzeOutcome {
	if (result.claimed.includes(stableId)) {
		return { message: 'Vocal analysis done', toastKind: 'info', refresh: true, category: null };
	}
	const category = Object.hasOwn(result.refused, stableId) ? result.refused[stableId] : null;
	if (category === 'cached_demucs') {
		return { message: vocalsRefusalCopy(category), toastKind: 'info', refresh: true, category };
	}
	return { message: vocalsRefusalCopy(category), toastKind: 'error', refresh: false, category };
}
