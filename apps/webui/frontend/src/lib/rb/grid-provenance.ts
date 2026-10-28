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
import type { AnlzData } from './anlz-types';

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

/** A grid provenance row: the short value shown, and the hover explaining it. */
export interface GridProvenanceRow {
	value: string;
	title: string;
}

/**
 * Sol P1 4301959432: a library track's Beatgrid row, a short value with the
 * full source and confidence in its hover title, like the USB stick row.
 */
export function libraryGridProvenance(view: GridProvenanceView): GridProvenanceRow {
	const title = gridProvenanceHoverText(view);
	if (view.state === 'idle') return { value: 'not loaded', title };
	else if (view.state === 'loading') return { value: 'loading', title };
	else if (view.state === 'error') return { value: 'unavailable', title };
	else if (view.state === 'ready') {
		if (view.value.source === 'rekordbox') return { value: 'rekordbox analysis', title };
		else if (view.value.source === 'own') {
			const value = view.value.status === 'ok' ? 'own analysis' : `own analysis, ${view.value.status}`;
			return { value, title };
		}
		const unhandledSource: never = view.value.source;
		throw new Error(`Unhandled: ${unhandledSource}`);
	}
	const unhandled: never = view;
	throw new Error(`Unhandled: ${JSON.stringify(unhandled)}`);
}

/** The deck fields a USB stick track's grid provenance is read from. */
export interface UsbGridDeck {
	stable_id: string | null;
	anlz: Pick<AnlzData, 'stable_id' | 'beatgrid' | 'beatgrid_source'> | null;
	anlz_error: string | null;
}

/**
 * Sol P1 4284394464: a `usb-*` track's beatgrid provenance, read from the
 * stick ANLZ the deck already loaded (GET /api/v1/usb/tracks/{id}/anlz,
 * always `beatgrid_source: 'rekordbox'`, usb_tracks.py `_anlz_payload`).
 * The library-only /tracks/{id}/grid-provenance read must never be asked
 * about a stick id: it treats an unmapped id as own analysis and misreports.
 * While the deck holds no ANLZ for this exact track the row says
 * unavailable and why, never a guess.
 */
export function usbGridProvenance(stableId: string, deck: UsbGridDeck): GridProvenanceRow {
	const onDeck = deck.stable_id === stableId;
	if (onDeck && deck.anlz !== null && deck.anlz.stable_id === stableId) {
		if (deck.anlz.beatgrid_source !== 'rekordbox') {
			throw new Error(
				`grid provenance: USB stick ANLZ for ${stableId} served beatgrid_source ` +
					`${String(deck.anlz.beatgrid_source)}; a stick has only its rekordbox grid`
			);
		}
		if (deck.anlz.beatgrid.beat_count === 0) {
			return {
				value: 'none on the USB stick',
				title:
					"Grid source: the USB stick's rekordbox export carries no beatgrid for this track " +
					'(rekordbox never analyzed it before export). Confidence: none, nothing was measured.'
			};
		}
		return {
			value: 'rekordbox analysis (USB stick)',
			title:
				`Grid source: rekordbox analysis read from the USB stick's ANLZ that this deck loaded ` +
				`(${deck.anlz.beatgrid.beat_count} beats). Confidence: rekordbox does not publish one.`
		};
	}
	if (onDeck && deck.anlz_error !== null) {
		return {
			value: 'unavailable',
			title:
				`Grid source unavailable: the deck could not read this USB stick track's ANLZ ` +
				`(${deck.anlz_error}). The library grid-provenance read does not cover stick tracks.`
		};
	}
	return {
		value: 'unavailable',
		title:
			"Grid source unavailable: this USB stick track's ANLZ has not loaded on the deck yet. " +
			'The library grid-provenance read does not cover stick tracks, so it is not asked.'
	};
}
