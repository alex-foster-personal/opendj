/**
 * Stem cache disk health dot policy (STEM-43). Pure, and the ONLY place the
 * low-disk dot's state and wording are decided.
 *
 * Same split as `library-health-dots.ts`: the component fetches
 * `GET /api/v1/stems/cache/status` and decides nothing; every threshold and
 * wording choice lives here, where a unit test can import and call the exact
 * function the component runs.
 *
 * The engine keeps a floor of disk free (20 GiB or 5% of the volume,
 * whichever is larger) by evicting least-recently-used stem bundles, but
 * only bundles the R2 index holds byte for byte. So "low disk" has three
 * different meanings and the dot must not blur them:
 *
 *   - low and being relieved: the next engine tick evicts. Amber.
 *   - low and PAUSED: eviction is switched off in settings. Nothing is
 *     broken and one switch relieves it, so amber, with what turning it
 *     back on would evict.
 *   - low and BLOCKED: nothing can be evicted, and the user has to act. Red.
 *
 * Every low-disk hover says what eviction removes and that those bundles
 * stay in the cloud: an eviction the user cannot predict reads as data loss.
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
	would_evict_count: number;
	would_evict_bytes: number;
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
	not_enough_evictable_bundles:
		'cannot reach the floor: the remaining stems are on a deck or not yet uploaded'
};

/** Paused is a choice, not a fault: amber. Every other blocker is red. */
const PAUSED_REASON = 'auto_evict_off';

/** What reaching the floor removes, and that it is not lost. */
function evictionPart(status: StemCacheStatusView): string {
	const count = status.would_evict_count;
	if (count <= 0) return 'no stem bundle is safe to evict';
	return `the ${count} least recently used stem ${count === 1 ? 'bundle' : 'bundles'} (${formatGib(status.would_evict_bytes)}); they stay available from the cloud and download again when loaded on a deck`;
}

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
		// Grey, never red: the endpoint could not answer, so nothing was measured.
		return { label, state: 'unavailable', detail: `unknown - ${loadError}` };
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
	if (status.blocked_reason === PAUSED_REASON) {
		return {
			label,
			state: 'incomplete',
			detail: `${short}; automatic eviction is paused in settings, nothing evicted. Turning it on would evict ${evictionPart(status)}${localOnly}`
		};
	}
	if (status.blocked_reason !== null) {
		const why = BLOCKED_WORDING[status.blocked_reason] ?? `blocked: ${status.blocked_reason}`;
		return { label, state: 'error', detail: `${short}; ${why}${localOnly}` };
	}
	return {
		label,
		state: 'incomplete',
		detail: `${short}; evicting ${evictionPart(status)}${localOnly}`
	};
}
