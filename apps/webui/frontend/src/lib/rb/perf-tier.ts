/**
 * Machine performance tier scalers (PERFMODE-01).
 * must match apps/shared/perf_tier.py SCALERS
 */

const MiB = 1024 * 1024;

export type PerfTierName = 'LOW' | 'STANDARD' | 'HIGH';
export type PerfTierSource = 'pending-host' | 'auto' | 'override';

/** must match apps/shared/perf_tier.py SCALERS */
export const SCALERS: Record<
	PerfTierName,
	{
		prefetch_tracks: number;
		prefetch_bytes: number;
		/** Decoded PCM the library preview may hold (CUEOUT-15). Decoded, not
		 * compressed: measured at about 0.34 MB per second of audio, so this
		 * buys far fewer tracks than `prefetch_bytes` of the same size. */
		preview_pcm_bytes: number;
		anlz_entries: number;
		anlz_bytes: number;
		stem_waveform_entries: number;
		stem_waveform_bytes: number;
		stem_decode: 'mix-only' | 'mix-first' | 'eager';
		worker_divisor: number;
	}
> = {
	LOW: {
		prefetch_tracks: 2,
		prefetch_bytes: 24 * MiB,
		preview_pcm_bytes: 64 * MiB,
		anlz_entries: 8,
		anlz_bytes: 10 * MiB,
		stem_waveform_entries: 12,
		stem_waveform_bytes: 128 * MiB,
		stem_decode: 'mix-only',
		worker_divisor: 2
	},
	STANDARD: {
		prefetch_tracks: 4,
		prefetch_bytes: 48 * MiB,
		preview_pcm_bytes: 160 * MiB,
		anlz_entries: 32,
		anlz_bytes: 40 * MiB,
		stem_waveform_entries: 24,
		stem_waveform_bytes: 256 * MiB,
		stem_decode: 'mix-first',
		worker_divisor: 1
	},
	HIGH: {
		prefetch_tracks: 6,
		prefetch_bytes: 96 * MiB,
		preview_pcm_bytes: 256 * MiB,
		anlz_entries: 64,
		anlz_bytes: 80 * MiB,
		stem_waveform_entries: 36,
		stem_waveform_bytes: 512 * MiB,
		stem_decode: 'eager',
		worker_divisor: 1
	}
};

let _tier: PerfTierName = 'STANDARD';
let _source: PerfTierSource = 'pending-host';
let _autoTier: PerfTierName | null = null;
let _hostCpus: number | null = null;
let _hostRamGiB: number | null = null;
let _faultReason: string | null = null;

export function activeScalers(): (typeof SCALERS)[PerfTierName] {
	return SCALERS[_tier];
}

export function resolvedTier(): PerfTierName {
	return _tier;
}

export function resolvedTierSource(): PerfTierSource {
	return _source;
}

export function perfTierFaultReason(): string | null {
	return _faultReason;
}

export function setResolvedTier(
	tier: PerfTierName,
	source: PerfTierSource,
	autoTier: PerfTierName | null = null,
	host?: { logical_cpus: number; ram_bytes: number } | null,
	faultReason: string | null = null
): void {
	_tier = tier;
	_source = source;
	_autoTier = autoTier;
	_faultReason = faultReason;
	if (host !== undefined) {
		_hostCpus = host?.logical_cpus ?? null;
		_hostRamGiB = host ? Math.round(host.ram_bytes / (1024 ** 3)) : null;
	}
}

export function prefetchTrackCap(): number {
	return activeScalers().prefetch_tracks;
}

export function prefetchByteCap(): number {
	return activeScalers().prefetch_bytes;
}

export function anlzEntryCap(): number {
	return activeScalers().anlz_entries;
}

export function anlzByteCap(): number {
	return activeScalers().anlz_bytes;
}

export function stemWaveformEntryCap(): number {
	return activeScalers().stem_waveform_entries;
}

export function stemWaveformByteCap(): number {
	return activeScalers().stem_waveform_bytes;
}

export function stemDecodeEagerness(): 'mix-only' | 'mix-first' | 'eager' {
	return activeScalers().stem_decode;
}

export function tierHoverSuffix(): string {
	const autoPart =
		_source === 'override'
			? 'Override'
			: _source === 'auto'
				? 'Auto'
				: 'Pending host';
	const hostPart =
		_hostCpus !== null && _hostRamGiB !== null
			? `; ${_hostCpus} cpus, ${_hostRamGiB} GiB`
			: '';
	const faultPart = _faultReason ? `. ${_faultReason}` : '';
	return (
		`Active tier: ${_tier} (${autoPart}${hostPart}). ` +
		`Budgets stay; caches shrink on Low.${faultPart}`
	);
}

export function tierLabelForHover(): string {
	return _tier;
}

export function mapPrefToTier(pref: string): PerfTierName | null {
	switch (pref) {
		case 'low':
			return 'LOW';
		case 'standard':
			return 'STANDARD';
		case 'high':
			return 'HIGH';
		default:
			return null;
	}
}
