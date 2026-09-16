/**
 * Fetch resolved perf tier from the engine chassis.
 */

import { API_BASE } from '$lib/api/base';
import { applyPreviewCaps } from '$lib/player/preview-cue.svelte';
import { applyPrefetchCaps } from '$lib/rb/audio-prefetch-cache.svelte';
import { applyAnlzCaps } from '$lib/components/rb/wave/anlz-cache-caps';
import {
	mapPrefToTier,
	SCALERS,
	setResolvedTier,
	type PerfTierName
} from '$lib/rb/perf-tier';

function _isPerfTierName(value: unknown): value is PerfTierName {
	return typeof value === 'string' && Object.hasOwn(SCALERS, value);
}

function _applyTierCaps(): void {
	applyPrefetchCaps();
	applyAnlzCaps();
	applyPreviewCaps();
}

export const PERF_TIER_PATH = '/api/v1/perf-tier';

interface PerfTierResponse {
	tier: PerfTierName;
	source: 'auto' | 'override';
	auto_tier: PerfTierName | null;
	override: string;
	host?: { logical_cpus: number; ram_bytes: number };
}

export async function fetchPerfTier(): Promise<void> {
	const url = `${API_BASE}${PERF_TIER_PATH}`;
	const response = await fetch(url);
	if (response.status === 503) {
		const body = (await response.json()) as { message?: string; error?: string };
		const reason =
			`tier unavailable: ${body.error ?? 'perf_tier_unavailable'} ` +
			`(${body.message ?? 'host facts unavailable'}); using Standard caps`;
		setResolvedTier('STANDARD', 'pending-host', null, null, reason);
		_applyTierCaps();
		return;
	}
	if (!response.ok) {
		setResolvedTier(
			'STANDARD',
			'pending-host',
			null,
			null,
			`tier unavailable: HTTP ${response.status}; using Standard caps`
		);
		_applyTierCaps();
		return;
	}
	const body = (await response.json()) as PerfTierResponse;
	if (!_isPerfTierName(body?.tier)) {
		throw new Error(
			`perf-tier: 200 from ${PERF_TIER_PATH} carried no valid tier ` +
				`(expected one of ${Object.keys(SCALERS).join(', ')}); body: ${JSON.stringify(body)}`
		);
	}
	setResolvedTier(
		body.tier,
		body.source,
		body.auto_tier,
		body.host ?? null,
		null
	);
	_applyTierCaps();
}

export function applyExplicitPerfTierPref(pref: string): void {
	if (pref === 'auto') {
		void fetchPerfTier();
		return;
	}
	const mapped = mapPrefToTier(pref);
	if (mapped !== null) {
		setResolvedTier(mapped, 'override', null);
		_applyTierCaps();
	}
}
