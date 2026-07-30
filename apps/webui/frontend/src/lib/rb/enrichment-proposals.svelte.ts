/**
 * Client-side enrichment proposal overlay (demo / until BE attaches).
 * When the listing payload gains `row.enrichment`, prefer that; this store
 * is the temporary map keyed by stable_id for DEV demos without a BE.
 */

import type { EnrichmentField, EnrichmentStatus, RowEnrichment } from './enrichment-display';

export type { EnrichmentField, EnrichmentStatus, RowEnrichment };

// TODO(enrich): BC3 dirty-metadata skill + eval set
let byId = $state<Record<string, RowEnrichment>>({});

function _cloneField(field: EnrichmentField): EnrichmentField {
	return { value: field.value, status: field.status };
}

function _cloneBag(bag: RowEnrichment): RowEnrichment {
	const next: RowEnrichment = {};
	if (bag.title) next.title = _cloneField(bag.title);
	if (bag.artist) next.artist = _cloneField(bag.artist);
	return next;
}

/** DEV build or `?simulateEnrich=1` - gates console / window demo helpers. */
export function enrichSimulateAllowed(
	search = typeof globalThis !== 'undefined' &&
		typeof (globalThis as { location?: { search?: string } }).location?.search === 'string'
		? (globalThis as { location: { search: string } }).location.search
		: ''
): boolean {
	try {
		if (import.meta.env?.DEV) return true;
	} catch {
		/* bundler without import.meta.env */
	}
	return new URLSearchParams(search).get('simulateEnrich') === '1';
}

export const enrichmentProposals = {
	get map(): Record<string, RowEnrichment> {
		return byId;
	},

	/** Lookup overlay for a stable_id (undefined when none). */
	for(stableId: string): RowEnrichment | undefined {
		return byId[stableId];
	},

	/**
	 * Prefer BE-attached `row.enrichment` when present; else client overlay.
	 * Stub until listing payloads carry enrichment from the daemon.
	 */
	resolve(row: { stable_id: string; enrichment?: RowEnrichment | null }): RowEnrichment | undefined {
		if (row.enrichment !== undefined && row.enrichment !== null) {
			return row.enrichment;
		}
		return byId[row.stable_id];
	},

	/** Upsert proposed/approved fields for a track (partial merge). */
	set(stableId: string, patch: RowEnrichment): void {
		const prev = byId[stableId] ?? {};
		const next: RowEnrichment = { ...prev };
		if (patch.title) next.title = _cloneField(patch.title);
		if (patch.artist) next.artist = _cloneField(patch.artist);
		byId = { ...byId, [stableId]: next };
	},

	/** Mark a field approved (keeps value). */
	approve(stableId: string, field: 'title' | 'artist'): void {
		const prev = byId[stableId];
		const cur = prev?.[field];
		if (!cur) return;
		this.set(stableId, { [field]: { value: cur.value, status: 'approved' } });
	},

	clear(stableId?: string): void {
		if (stableId === undefined) {
			byId = {};
			return;
		}
		if (!(stableId in byId)) return;
		const next = { ...byId };
		delete next[stableId];
		byId = next;
	},

	/**
	 * DEV / `?simulateEnrich=1`: propose an orange title for demo.
	 * Example: `__enrichmentPropose(stableId, 'Clean Title (proposed)')`
	 */
	simulatePropose(
		stableId: string,
		title: string,
		opts?: { artist?: string; status?: EnrichmentStatus }
	): void {
		if (!enrichSimulateAllowed()) {
			console.warn('[enrichment] simulate blocked (need DEV or ?simulateEnrich=1)');
			return;
		}
		const status = opts?.status ?? 'proposed';
		const patch: RowEnrichment = {
			title: { value: title, status }
		};
		if (opts?.artist !== undefined && opts.artist !== '') {
			patch.artist = { value: opts.artist, status };
		}
		this.set(stableId, patch);
	}
};

function _bindSimulateGlobal(): void {
	if (typeof window === 'undefined') return;
	if (!enrichSimulateAllowed()) return;
	(
		window as unknown as {
			__enrichmentPropose?: (
				stableId: string,
				title: string,
				opts?: { artist?: string; status?: EnrichmentStatus }
			) => void;
			__enrichmentApprove?: (stableId: string, field?: 'title' | 'artist') => void;
			__enrichmentClear?: (stableId?: string) => void;
		}
	).__enrichmentPropose = (stableId, title, opts) => {
		enrichmentProposals.simulatePropose(stableId, title, opts);
	};
	(
		window as unknown as {
			__enrichmentApprove?: (stableId: string, field?: 'title' | 'artist') => void;
		}
	).__enrichmentApprove = (stableId, field = 'title') => {
		enrichmentProposals.approve(stableId, field);
	};
	(
		window as unknown as {
			__enrichmentClear?: (stableId?: string) => void;
		}
	).__enrichmentClear = (stableId) => {
		enrichmentProposals.clear(stableId);
	};
}

_bindSimulateGlobal();

/** Snapshot clone for tests. */
export function _enrichmentSnapshot(): Record<string, RowEnrichment> {
	const out: Record<string, RowEnrichment> = {};
	for (const [id, bag] of Object.entries(byId)) {
		out[id] = _cloneBag(bag);
	}
	return out;
}
