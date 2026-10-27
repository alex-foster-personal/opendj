/**
 * BPM column cell text (operator report, Sat 24 Oct 2026: "many tracks show alt").
 *
 * `available-not-selected` means "rekordbox is the selected beatgrid source,
 * and our own analysis ALSO exists". The served value is still rekordbox's
 * BPM, so that row has a real number to show. The cell used to print the
 * literal word "alt" for it instead: 1,023 of 8,174 rows on the reporting operator's library.
 * The status now only adds a sentence to the hover; the cell shows the BPM.
 *
 * A row with no usable BPM prints MISSING_BPM_TEXT. rekordbox stores an
 * unanalysed track's BPM as 0 (apps/shared/state/ingest/rekordbox.py), which
 * is no tempo at all, so 0 and below count as missing rather than "0".
 */
import type { BrowserRow } from '$lib/components/rb/browser/pane-contract.svelte';

export const MISSING_BPM_TEXT = '--';

export type BpmCellKind = 'value' | 'missing' | 'failed' | 'not-analyzed';

export interface BpmCellText {
	text: string;
	kind: BpmCellKind;
}

type BpmCellRow = Pick<BrowserRow, 'bpm' | 'bpm_status'>;

export function hasUsableBpm(bpm: number | null): bpm is number {
	return bpm !== null && Number.isFinite(bpm) && bpm > 0;
}

function formatBpm(bpm: number | null): string {
	// One decimal, the same precision the hover's "Exact BPM" uses: a served
	// 123.6 must not read as 124 (review P2, PR #7483).
	return hasUsableBpm(bpm) ? bpm.toFixed(1) : MISSING_BPM_TEXT;
}

export function bpmCellText(row: BpmCellRow): BpmCellText {
	switch (row.bpm_status) {
		case 'failed':
			return { text: 'failed', kind: 'failed' };
		case 'missing':
			return { text: 'missing', kind: 'not-analyzed' };
		// An absent status is "ok, compacted off the wire" (browser-row-wire.ts
		// listingLaneStatus); the value still decides what the cell shows.
		case undefined:
		case 'ok':
		case 'available-not-selected':
			return hasUsableBpm(row.bpm)
				? { text: formatBpm(row.bpm), kind: 'value' }
				: { text: MISSING_BPM_TEXT, kind: 'missing' };
		default: {
			const exhaustive: never = row.bpm_status;
			throw new Error(`Unhandled bpm_status: ${String(exhaustive)}`);
		}
	}
}
