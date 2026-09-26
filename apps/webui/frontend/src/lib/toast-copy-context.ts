/**
 * Extra lines for toast clipboard copy: caller context, deck BPM/sync on
 * /performance, and the newest perf-log error row when available.
 */

import type { ClientErrorContext } from '$lib/client-error-reporting';
import { isPerformanceRoutePath } from '$lib/rb/performance-preset';

export interface ToastCopyDeckSnapshot {
	id: number;
	stable_id: string | null;
	bpm: number | null;
	beat_sync_enabled: boolean;
	sync_mode: string;
	is_master: boolean;
}

const STABLE_ID_CLIP = 24;

function _clipStableId(stableId: string | null): string {
	if (stableId === null || stableId === '') return 'none';
	if (stableId.length <= STABLE_ID_CLIP) return stableId;
	return `${stableId.slice(0, STABLE_ID_CLIP)}…`;
}

function _syncLabel(deck: ToastCopyDeckSnapshot): string {
	if (!deck.beat_sync_enabled) return 'off';
	return deck.sync_mode;
}

function _flattenContext(reportContext?: ClientErrorContext): Record<string, string> {
	if (reportContext === undefined) return {};
	const out: Record<string, string> = {};
	for (const [key, value] of Object.entries(reportContext)) {
		if (value === undefined || value === null) continue;
		out[`ctx_${key}`] = String(value);
	}
	return out;
}

function _deckLines(decks: readonly ToastCopyDeckSnapshot[]): Record<string, string> {
	const out: Record<string, string> = {};
	const master = decks.find((deck) => deck.is_master);
	if (master !== undefined) out.master_deck = String(master.id);
	for (const deck of decks) {
		const bpm = deck.bpm === null ? '?' : String(Math.round(deck.bpm));
		out[`deck_${deck.id}`] =
			`stable=${_clipStableId(deck.stable_id)} bpm=${bpm} sync=${_syncLabel(deck)}`;
	}
	return out;
}

export function gatherToastCopyExtras(input: {
	pathname: string;
	reportContext?: ClientErrorContext;
	readDecks?: () => readonly ToastCopyDeckSnapshot[];
	readLastError?: () => { kind: string; message: string } | null;
}): Record<string, string> {
	const extras = _flattenContext(input.reportContext);
	if (isPerformanceRoutePath(input.pathname) && input.readDecks !== undefined) {
		Object.assign(extras, _deckLines(input.readDecks()));
	}
	if (isPerformanceRoutePath(input.pathname) && input.readLastError !== undefined) {
		const last = input.readLastError();
		if (last !== null) extras.last_perf_error = `${last.kind}: ${last.message}`;
	}
	return extras;
}
