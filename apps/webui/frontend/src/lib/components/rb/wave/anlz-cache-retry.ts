/** Retryable /anlz entry classification (issue #735 follow-up). */

import type { AnlzData } from '$lib/rb/anlz-types';

export type AnlzRetryEntry =
	| { status: 'loading' | 'error' }
	| { status: 'ready'; data: AnlzData; retryAfter?: number; touched?: number };

function _isRetryableEntry(
	entry: Extract<AnlzRetryEntry, { status: 'ready' }>
): boolean {
	if (entry.data.local_waveform?.status !== 'not_decoded') return false;
	return entry.data.local_waveform.retryable === true;
}

function _isDueForRetry(entry: Extract<AnlzRetryEntry, { status: 'ready' }>): boolean {
	return entry.retryAfter === undefined || performance.now() >= entry.retryAfter;
}

export function dueForEnsureRefetch(entry: AnlzRetryEntry): boolean {
	if (entry.status !== 'ready') return false;
	return _isRetryableEntry(entry) && _isDueForRetry(entry);
}

export function isRetryableAnlzData(data: AnlzData): boolean {
	return data.local_waveform?.status === 'not_decoded' && data.local_waveform.retryable === true;
}

export function isAnlzEntryUsable(
	entry: AnlzRetryEntry | undefined
): entry is Extract<AnlzRetryEntry, { status: 'ready' }> {
	if (entry === undefined || entry.status !== 'ready') return false;
	return !_isRetryableEntry(entry);
}
