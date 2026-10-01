/**
 * Stem cache disk health dot policy (STEM-43). Pure, and the ONLY place the
 * low-disk dot's state and wording are decided.
 *
 * Same split as `library-health-dots.ts`: the component fetches
 * `GET /api/v1/stems/cache/status` and decides nothing; every threshold and
 * wording choice lives here, where a unit test can import and call the exact
 * function the component runs.
 *
 * The engine keeps a floor of disk free (30 GiB or 10% of the volume,
 * whichever is larger) by evicting least-recently-used stem bundles, but
 * only bundles the R2 index holds byte for byte. So "low disk" has two very
 * different meanings and the dot must not blur them:
 *
 *   - low and being relieved: the next engine tick evicts. Amber.
 *   - low and BLOCKED: nothing can be evicted, and the user has to act. Red.
 */

export type StemCacheHealthDot = {
	label: 'Stem cache disk';
	state: 'loading' | 'complete' | 'incomplete' | 'unavailable' | 'error';
	detail: string;
};

/** The fields of `StemCacheStatusOut` this policy reads. */
export type StemCacheStatusView = {
	state: 'healthy' | 'low_disk';
	disk_free_bytes: number;
	floor_bytes: number;
	shortfall_bytes: number;
	cache_bytes: number;
	bundle_count: number;
	local_only_count: number;
	blocked_reason: string | null;
	last_error: string | null;
};

const GIB = 1024 ** 3;

/** One decimal under 10 GiB, whole numbers above: 4.2 GiB, 46 GiB. */
export function formatGib(bytes: number): string {
	const gib = bytes / GIB;
	return `${gib < 10 ? gib.toFixed(1) : Math.round(gib).toString()} GiB`;
}

const BLOCKED_WORDING: Record<string, string> = {
	hydration_not_armed:
		'nothing evicted: this machine cannot fetch stems back from the cloud yet (CloudSync is off or not enrolled)',
	auto_evict_off: 'nothing evicted: automatic eviction is turned off in settings',
	not_enough_evictable_bundles:
		'cannot reach the floor: the remaining stems are on a deck or not yet uploaded'
};

function localOnlyPart(count: number): string {
	if (count <= 0) return '';
	return `, ${count} ${count === 1 ? 'bundle' : 'bundles'} not yet uploaded (kept, queued for upload)`;
}

export function stemCacheHealthDot(
	status: StemCacheStatusView | null,
	loadError: string | null
): StemCacheHealthDot {
	const label = 'Stem cache disk' as const;
	if (loadError !== null) {
		return { label, state: 'error', detail: loadError };
	}
	if (status === null) {
		return { label, state: 'loading', detail: 'checking disk space' };
	}
	if (status.last_error !== null) {
		return { label, state: 'error', detail: `disk check failed: ${status.last_error}` };
	}
	const free = `${formatGib(status.disk_free_bytes)} free, floor ${formatGib(status.floor_bytes)}`;
	const cache = `stems use ${formatGib(status.cache_bytes)} in ${status.bundle_count} ${status.bundle_count === 1 ? 'bundle' : 'bundles'}`;
	const localOnly = localOnlyPart(status.local_only_count);
	if (status.state === 'healthy') {
		return { label, state: 'complete', detail: `${free}; ${cache}${localOnly}` };
	}
	const short = `low disk: ${formatGib(status.shortfall_bytes)} short (${free})`;
	if (status.blocked_reason !== null) {
		const why = BLOCKED_WORDING[status.blocked_reason] ?? `blocked: ${status.blocked_reason}`;
		return { label, state: 'error', detail: `${short}; ${why}${localOnly}` };
	}
	return {
		label,
		state: 'incomplete',
		detail: `${short}; evicting least recently used stems that are safe in the cloud${localOnly}`
	};
}
