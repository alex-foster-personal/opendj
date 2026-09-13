/**
 * Pure decisions behind the /cloudsync UI (plan W17 + the UI half of W5).
 *
 * Every rule the components apply lives here so a unit test can pin it
 * without a DOM: what an unset policy cell shows, when a budget edit is
 * refused, what the status chip reads, and what a Sync-now or config save
 * would send. Nothing here fetches; the components call the typed clients
 * in `$lib/api-cloudsync` and `$lib/api-cloudsync-ops` with the bodies these
 * functions return.
 *
 * The one hard rule (hydration_core.resolve_policy): the backend REFUSES a
 * missing policy row rather than defaulting it. So the UI never invents a
 * mode either. An unset cell reads 'unset', and nothing derives 'stream'
 * (or any mode) from an absent row.
 */

import {
	SYNC_MODES,
	type AssetKind,
	type CloudSyncStatus,
	type SyncMode,
	type SyncPolicy,
	type SyncPolicyPutBody
} from '$lib/api-cloudsync';
import type { CloudSyncConfigBody, CloudSyncConfigOut, SyncRunBody } from '$lib/api-cloudsync-ops';

export const INERT_TOOLTIP = 'not implemented - see PARITY-TODO';

/** The explicit display value of a policy cell with no stored row. */
export const UNSET = 'unset' as const;
export type PolicyCellMode = SyncMode | typeof UNSET;

/**
 * Asset kinds whose stored policy something in production actually acts on.
 * Only karaoke_words has a policy-driven producer today
 * (apps/lyrics/artifacts.py pushes through apps.cloud.asset_store). For the
 * rest, resolve_playback_source / apply_policy_after_produce / evict_cache
 * have no production caller, so a stored mode syncs but changes nothing on
 * disk. Those columns are marked inert, never hidden.
 */
export const RUNTIME_WIRED_ASSET_KINDS: ReadonlyArray<AssetKind> = ['karaoke_words'];

export function isRuntimeWired(kind: AssetKind): boolean {
	return RUNTIME_WIRED_ASSET_KINDS.includes(kind);
}

export function inertKindTitle(kind: AssetKind): string | null {
	return isRuntimeWired(kind)
		? null
		: `The ${kind} policy is stored and synced, but no production code acts on it yet: ${INERT_TOOLTIP}`;
}

// ----------------------------------------------------------- policy matrix

export interface PolicyCellView {
	mode: PolicyCellMode;
	budgetEditable: boolean;
	budgetTitle: string;
}

export function policyCellView(policy: SyncPolicy | undefined): PolicyCellView {
	if (policy === undefined) {
		return {
			mode: UNSET,
			budgetEditable: false,
			budgetTitle: 'No policy is stored for this cell. Pick a mode first; a budget applies only in cached mode.'
		};
	}
	if (policy.mode !== 'cached') {
		return {
			mode: policy.mode,
			budgetEditable: false,
			budgetTitle: `Cache budget only applies in cached mode (stored mode is ${policy.mode}).`
		};
	}
	return {
		mode: 'cached',
		budgetEditable: true,
		budgetTitle: 'Local hot-cache budget in MB (LRU eviction above this).'
	};
}

export type PolicyPutDecision =
	| { kind: 'put'; body: SyncPolicyPutBody }
	| { kind: 'refuse'; reason: string };

function isSyncMode(raw: string): raw is SyncMode {
	return (SYNC_MODES as ReadonlyArray<string>).includes(raw);
}

/** A mode pick on one cell. 'unset' is display only: no route clears a row. */
export function policyModeChange(
	machineId: string,
	assetKind: AssetKind,
	existing: SyncPolicy | undefined,
	raw: string
): PolicyPutDecision {
	if (raw === UNSET) {
		return {
			kind: 'refuse',
			reason: `A stored policy cannot be cleared back to unset yet (no DELETE route): ${INERT_TOOLTIP}`
		};
	}
	if (!isSyncMode(raw)) {
		return { kind: 'refuse', reason: `Unknown sync mode ${JSON.stringify(raw)}` };
	}
	return {
		kind: 'put',
		body: {
			machine_id: machineId,
			asset_kind: assetKind,
			mode: raw,
			cache_budget_mb: raw === 'cached' ? (existing?.cache_budget_mb ?? null) : null
		}
	};
}

/** A budget edit on one cell. Refused unless the STORED mode is cached. */
export function policyBudgetChange(
	machineId: string,
	assetKind: AssetKind,
	existing: SyncPolicy | undefined,
	raw: string
): PolicyPutDecision {
	if (existing === undefined) {
		return {
			kind: 'refuse',
			reason: 'This cell has no stored policy. Pick a mode first; an unset cell is never saved as stream.'
		};
	}
	if (existing.mode !== 'cached') {
		return {
			kind: 'refuse',
			reason: `Cache budget only applies in cached mode; the stored mode is ${existing.mode}.`
		};
	}
	let budget: number | null = null;
	if (raw.trim() !== '') {
		const parsed = Number(raw);
		if (!Number.isInteger(parsed) || parsed < 0) {
			return { kind: 'refuse', reason: 'Cache budget must be a whole number of MB, 0 or more.' };
		}
		budget = parsed;
	}
	return {
		kind: 'put',
		body: { machine_id: machineId, asset_kind: assetKind, mode: 'cached', cache_budget_mb: budget }
	};
}

// ----------------------------------------------------------- status chip

export type ChipState = 'off' | 'syncing' | 'ok' | 'error' | 'inconclusive';

/**
 * How often the chip re-reads GET /cloudsync/status (skipped while the tab
 * is hidden). Kept under the heartbeat's STALE_AFTER_S (45 s,
 * apps/sync_hub/heartbeat.py) so a scheduler that dies after page load reads
 * 'off' within one stale window plus one poll, not only after a reload.
 */
export const CHIP_POLL_MS = 30_000;

/** Fired on `window` after Sync now or a config save, so the chip re-reads at once. */
export const STATUS_CHANGED_EVENT = 'cloudsync:status-changed';

/**
 * Off unless a scheduler heartbeat is fresh. `running` is the evidence;
 * `configured` is only intent, so neither config nor an old ok result can
 * light the chip without a live loop.
 */
export function chipState(status: CloudSyncStatus | null): ChipState {
	if (status === null || !status.configured || !status.running) return 'off';
	if (status.last_result?.status === 'error') return 'error';
	// Its own state, never folded into 'ok': the sync completed but its
	// digest compare excluded rows, so agreement was not verified.
	if (status.last_result?.status === 'inconclusive') return 'inconclusive';
	if (status.last_result?.status === 'ok') return 'ok';
	return 'syncing';
}

export function relativeTime(value: string | null, nowMs: number = Date.now()): string {
	if (value === null) return 'never';
	const delta = nowMs - Date.parse(value);
	if (!Number.isFinite(delta) || delta < 0) return value;
	const minutes = Math.floor(delta / 60_000);
	if (minutes < 1) return 'just now';
	if (minutes < 60) return `${minutes}m ago`;
	return `${Math.floor(minutes / 60)}h ago`;
}

export const CHIP_HREF = '/cloudsync';

export function chipFullLabel(status: CloudSyncStatus | null): string {
	const state = chipState(status);
	if (state === 'ok') return `sync: ok ${relativeTime(status?.last_push_at ?? null)}`;
	if (state === 'error') return 'sync: error';
	if (state === 'inconclusive')
		return `sync: inconclusive ${relativeTime(status?.last_push_at ?? null)}`;
	return `sync: ${state}`;
}

export function chipShortLabel(status: CloudSyncStatus | null): string {
	const state = chipState(status);
	if (state === 'off') return 'off';
	if (state === 'syncing') return 'sync';
	if (state === 'ok') return 'ok';
	if (state === 'error') return 'err';
	return 'inc';
}

export function chipTitle(status: CloudSyncStatus | null, loadError: string | null): string {
	if (status === null) {
		return loadError === null
			? 'CloudSync status - still loading from the daemon. Click after it loads to see details.'
			: `CloudSync status unavailable: ${loadError}. Click to retry details.`;
	}
	const state = chipState(status);
	const next = 'Click to open CloudSync.';
	if (state === 'off') {
		return `CloudSync is off${status.reason ? ` (${status.reason})` : ''}. ${next}`;
	}
	if (state === 'error') {
		return `CloudSync error: ${status.last_result?.message ?? 'last sync failed'}. ${next}`;
	}
	if (state === 'inconclusive') {
		return `CloudSync last run was inconclusive - agreement was not verified. ${next}`;
	}
	if (state === 'ok') {
		return `CloudSync last succeeded ${relativeTime(status.last_push_at)}. ${next}`;
	}
	return `CloudSync is syncing${status.rows_pending !== null ? ` (${status.rows_pending} rows pending)` : ''}. ${next}`;
}

// ----------------------------------------------------------- sync now

export type SyncNowDecision = { kind: 'post'; body: SyncRunBody } | { kind: 'refuse'; reason: string };

/** Sync now runs against the EFFECTIVE hub URL (env override included). */
export function syncNowRequest(config: CloudSyncConfigOut | null): SyncNowDecision {
	if (config === null) {
		return { kind: 'refuse', reason: 'CloudSync config has not loaded yet.' };
	}
	const hubUrl = config.effective.hub_url;
	if (hubUrl === null) {
		return { kind: 'refuse', reason: 'Set a hub URL in the config form first.' };
	}
	return { kind: 'post', body: { hub_url: hubUrl, name: config.effective.machine_name } };
}

// ----------------------------------------------------------- config form

export interface ConfigFormFields {
	enabled: boolean;
	hubUrl: string;
	machineName: string;
}

export type ConfigPutDecision =
	| { kind: 'put'; body: CloudSyncConfigBody }
	| { kind: 'refuse'; reason: string };

/** Mirrors apps.sync_hub.config.CloudSyncConfig so a bad form never round-trips. */
export function configPutBody(form: ConfigFormFields): ConfigPutDecision {
	const hubUrl = form.hubUrl.trim() === '' ? null : form.hubUrl.trim();
	const machineName = form.machineName.trim() === '' ? null : form.machineName.trim();
	if (hubUrl !== null && !/^https?:\/\/[^/\s]+/.test(hubUrl)) {
		return { kind: 'refuse', reason: `Hub URL must be an http(s) URL with a host, got ${JSON.stringify(hubUrl)}.` };
	}
	if (form.enabled && hubUrl === null) {
		return { kind: 'refuse', reason: 'Enabling CloudSync needs a hub URL.' };
	}
	return { kind: 'put', body: { enabled: form.enabled, hub_url: hubUrl, machine_name: machineName } };
}

export function formFromConfig(config: CloudSyncConfigOut): ConfigFormFields {
	return {
		enabled: config.file?.enabled ?? false,
		hubUrl: config.file?.hub_url ?? '',
		machineName: config.file?.machine_name ?? ''
	};
}

/** The env overrides masking what the file says, as operator-readable lines. */
export function envOverrideNotes(config: CloudSyncConfigOut): string[] {
	const notes: string[] = [];
	if (config.effective.enabled_source === 'env') {
		notes.push(
			`MDT_CLOUDSYNC_SCHEDULER overrides the saved 'enabled' (effective: ${config.effective.enabled}).`
		);
	}
	if (config.effective.hub_url_source === 'env') {
		notes.push(
			`MDT_CLOUDSYNC_HUB_URL overrides the saved hub URL (effective: ${config.effective.hub_url}).`
		);
	}
	return notes;
}

export type CloudSyncTab = 'status' | 'policies' | 'pins' | 'overview' | 'fleet';

export const CLOUDSYNC_TABS: ReadonlyArray<{ id: CloudSyncTab; label: string }> = [
	{ id: 'status', label: 'Status & config' },
	{ id: 'policies', label: 'Machines & policies' },
	{ id: 'pins', label: 'Playlist pins' },
	{ id: 'overview', label: 'Hydration overview' },
	{ id: 'fleet', label: 'Fleet' }
];

/** Resolve the visible /cloudsync tab from the current URL query string. */
export function cloudSyncTabFromUrl(url: URL): CloudSyncTab {
	const raw = url.searchParams.get('tab');
	return CLOUDSYNC_TABS.find((t) => t.id === raw)?.id ?? 'status';
}
