/**
 * Pure dropout policy for silence watchdog and device-unreachable verdicts
 * (issue #2069). Diagnosis, toast text, and recovery plan without engine imports.
 */

import type { DeckId } from '$lib/rb/deck-slots';
import type { SyncMode } from '$lib/rb/deck-state-types';
import type { PerfEvent } from '$lib/rb/perf-event-buckets';

export type SilenceCause = 'xrun' | 'processor-error' | 'stranded-follower' | 'unknown';

export interface SilenceDropoutDeckSnap {
	id: DeckId;
	playing: boolean;
	audible: boolean;
	bpm: number | null;
	beat_sync_enabled: boolean;
	sync_mode: SyncMode;
	sync_error: string | null;
	processor_error: string | null;
	is_master: boolean;
}

export interface SilenceDropoutPlan {
	stop_decks: DeckId[];
	toast: string;
	perf_kind: 'silent-while-playing';
	cause: SilenceCause;
	cause_message: string;
	autoplay_recover: boolean;
}

export interface DiagnoseSilenceCauseInput {
	decks: readonly SilenceDropoutDeckSnap[];
	events: readonly PerfEvent[];
	xruns?: number;
	xruns_at_previous?: number;
}

export interface FormatAudioCutToastInput {
	decks: readonly SilenceDropoutDeckSnap[];
	cause: SilenceCause;
	last_error: { kind: string; message: string } | null;
}

export interface PlanSilenceDropoutInput extends DiagnoseSilenceCauseInput {
	autoplay_enabled: boolean;
	has_playable_next: boolean;
}

const PROCESSOR_FAILED_RE = /processor failed/i;
const GRIDLESS_SYNC_RE = /settled without a beatgrid|gridless/i;

function _claimsLive(deck: SilenceDropoutDeckSnap): boolean {
	return deck.playing || deck.audible;
}

function _syncLabel(deck: SilenceDropoutDeckSnap): string {
	if (!deck.beat_sync_enabled) return 'off';
	return deck.sync_mode;
}

function _newestErrorEvent(events: readonly PerfEvent[]): PerfEvent | null {
	for (let i = events.length - 1; i >= 0; i--) {
		const row = events[i];
		const severity = row.severity ?? 'unknown';
		if (severity === 'error') return row;
	}
	return null;
}

export function lastPerfError(events: readonly PerfEvent[]): { kind: string; message: string } | null {
	const row = _newestErrorEvent(events);
	if (row === null) return null;
	return { kind: row.kind, message: row.message };
}

function _newestErrorKind(events: readonly PerfEvent[], kind: string): PerfEvent | null {
	for (let i = events.length - 1; i >= 0; i--) {
		const row = events[i];
		if (row.kind !== kind) continue;
		if ((row.severity ?? 'unknown') === 'error') return row;
	}
	return null;
}

export function diagnoseSilenceCause(input: DiagnoseSilenceCauseInput): SilenceCause {
	if (input.decks.some((deck) => deck.processor_error !== null)) return 'processor-error';
	if (_newestErrorKind(input.events, 'processor-error') !== null) return 'processor-error';
	for (let i = input.events.length - 1; i >= 0; i--) {
		const row = input.events[i];
		if (row.kind !== 'toast-error' || (row.severity ?? 'unknown') !== 'error') continue;
		if (PROCESSOR_FAILED_RE.test(row.message)) return 'processor-error';
	}
	if (_newestErrorKind(input.events, 'xrun') !== null) return 'xrun';
	if (
		input.xruns !== undefined &&
		input.xruns_at_previous !== undefined &&
		input.xruns > input.xruns_at_previous
	) {
		return 'xrun';
	}
	if (_newestErrorKind(input.events, 'stranded-follower') !== null) return 'stranded-follower';
	const master = input.decks.find((deck) => deck.is_master) ?? null;
	if (master !== null) {
		for (const deck of input.decks) {
			if (deck.id === master.id) continue;
			if (!deck.beat_sync_enabled || deck.sync_error === null) continue;
			if (!GRIDLESS_SYNC_RE.test(deck.sync_error)) continue;
			if (_claimsLive(master)) return 'stranded-follower';
		}
	}
	return 'unknown';
}

export function formatAudioCutToast(input: FormatAudioCutToastInput): string {
	const live = input.decks.filter(_claimsLive);
	const deckIds = live.map((deck) => String(deck.id)).join(',') || '?';
	const bpms = live.map((deck) => (deck.bpm === null ? '?' : String(Math.round(deck.bpm)))).join('/') || '?';
	const sync = live.map(_syncLabel).join('/') || 'off';
	const last =
		input.last_error === null
			? 'last=none'
			: `last=${input.last_error.kind}: ${input.last_error.message}`;
	return `AUDIO CUT decks=${deckIds} bpm=${bpms} sync=${sync} cause=${input.cause} ${last}`;
}

function _causeMessage(cause: SilenceCause, last: { kind: string; message: string } | null): string {
	const tail = last === null ? 'last=none' : `last=${last.kind}: ${last.message}`;
	return `silent while claimed live cause=${cause} ${tail}`;
}

export function planSilenceDropout(input: PlanSilenceDropoutInput): SilenceDropoutPlan {
	const cause = diagnoseSilenceCause(input);
	const last = lastPerfError(input.events);
	const liveDecks = input.decks.filter(_claimsLive).map((deck) => deck.id);
	return {
		stop_decks: liveDecks,
		toast: formatAudioCutToast({ decks: input.decks, cause, last_error: last }),
		perf_kind: 'silent-while-playing',
		cause,
		cause_message: _causeMessage(cause, last),
		autoplay_recover: input.autoplay_enabled && input.has_playable_next
	};
}
