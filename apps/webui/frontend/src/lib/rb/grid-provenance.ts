/**
 * Pin f85c5881: the sentence the BPM hover adds about where a track's
 * beatgrid came from, and how sure the analyzer was.
 *
 * Mini-PRD
 *   ✔︎ ✅ 🎯 Own analysis: name the backend and the stored 0..1 confidence,
 *     and say what the number means.
 *     [if] a confidence is printed without its scale [then ⛔️] broken
 *   ✔︎ ✅ 🎯 rekordbox: say so, and that it publishes no confidence.
 *     [if] a rekordbox grid shows an own-analysis figure [then ⛔️] broken
 *   ✔︎ ✅ 🎯 Missing, failed, loading and read-error are each worded.
 *     [if] the hover is blank or shows 0 for "never measured" [then ⛔️] broken
 *
 * Pure: the fetch and its cache live in grid-provenance.svelte.ts.
 */

import type { components } from '../api-types';

export type GridProvenance = components['schemas']['GridProvenanceOut'];

export type GridProvenanceView =
	| { state: 'idle' }
	| { state: 'loading' }
	| { state: 'error'; message: string }
	| { state: 'ready'; value: GridProvenance };

export type GridProvenanceEntry = Exclude<GridProvenanceView, { state: 'idle' }> & { at: number };

/** How long a read answer is reused. The answer moves when the analysis
 * source setting flips or a new own record lands, so it is not kept long. */
export const GRID_PROVENANCE_TTL_MS = 30_000;

export function gridProvenanceNeedsFetch(
	entry: GridProvenanceEntry | undefined,
	nowMs: number
): boolean {
	if (entry === undefined) return true;
	if (entry.state === 'loading') return false;
	else if (entry.state === 'error') return true;
	else if (entry.state === 'ready') return nowMs - entry.at >= GRID_PROVENANCE_TTL_MS;
	const unhandled: never = entry;
	throw new Error(`Unhandled: ${JSON.stringify(unhandled)}`);
}

function _ownText(value: GridProvenance): string {
	const why = value.basis === 'unmapped-default' ? ' (this track has no rekordbox analysis)' : '';
	if (value.status === 'ok') {
		if (value.bpm_confidence === null || value.bpm_confidence === undefined) {
			throw new Error('grid provenance: an ok own beatgrid must carry bpm_confidence');
		}
		return (
			`Grid source: own analysis, ${value.backend} ${value.backend_version}${why}. ` +
			`Confidence ${value.bpm_confidence.toFixed(2)} on a 0 to 1 scale: ` +
			`the analyzer's own score for its tempo estimate, higher is surer.`
		);
	} else if (value.status === 'missing' || value.status === 'failed') {
		return (
			`Grid source: own analysis, ${value.status}${why}: ${value.reason}. ` +
			`Confidence: none, nothing was measured.`
		);
	}
	throw new Error(`grid provenance: own source with status ${String(value.status)}`);
}

export function gridProvenanceHoverText(view: GridProvenanceView): string {
	if (view.state === 'idle') {
		return 'Grid source and confidence load when you hover this cell.';
	} else if (view.state === 'loading') {
		return 'Grid source and confidence: loading.';
	} else if (view.state === 'error') {
		return `Grid source and confidence could not be read: ${view.message}`;
	} else if (view.state === 'ready') {
		if (view.value.source === 'rekordbox') {
			return 'Grid source: rekordbox analysis. Confidence: rekordbox does not publish one.';
		} else if (view.value.source === 'own') {
			return _ownText(view.value);
		}
		const unhandledSource: never = view.value.source;
		throw new Error(`Unhandled: ${unhandledSource}`);
	}
	const unhandled: never = view;
	throw new Error(`Unhandled: ${JSON.stringify(unhandled)}`);
}
