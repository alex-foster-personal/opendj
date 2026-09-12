/**
 * Fetch resolved perf tier from the engine chassis.
 */

import { API_BASE } from '$lib/api/base';
import { applyPrefetchCaps } from '$lib/rb/audio-prefetch-cache.svelte';
import { applyAnlzCaps } from '$lib/components/rb/wave/anlz-cache-caps';
import {
	mapPrefToTier,
	setResolvedTier,
	type PerfTierName
} from '$lib/rb/perf-tier';

function _applyTierCaps(): void {
	applyPrefetchCaps();
	applyAnlzCaps();
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
