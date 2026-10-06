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

import { api, readApiErrorStatus } from '$lib/api/client';
import {
	SYNC_MODES,
	type AssetKind,
	type CloudSyncStatus,
	type CloudSyncUpdateRequired,
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
 * Audio deck load and karaoke_words have production callers today
 * (resolve_playback_source for audio; apps/lyrics/artifacts.py for
 * karaoke_words). For the rest, apply_policy_after_produce / evict_cache
 * have no production caller yet, so a stored mode syncs but changes nothing on
 * disk. Those columns are marked inert, never hidden.
 */
export const RUNTIME_WIRED_ASSET_KINDS: ReadonlyArray<AssetKind> = [
	'audio',
	'karaoke_words'
];

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

export type ChipState = 'off' | 'syncing' | 'ok' | 'error' | 'inconclusive' | 'update_required';

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
/**
 * The backend's reason prefix while a LIVE scheduler cannot reach the hub
 * and is retrying (apps/sync_hub/status.py WAITING_FOR_HUB_PREFIX, #3870).
 * A first run that was turned on by the build's default hub may start before
 * that hub answers; that is a wait, never a red error. The backend decides
 * (it needs the heartbeat and the transport classification); the UI mirrors.
 */
export const WAITING_FOR_HUB_PREFIX = 'Waiting for hub';

export function isWaitingForHub(status: CloudSyncStatus | null): boolean {
	return (
		status !== null &&
		status.configured &&
		status.running &&
		status.reason !== null &&
		status.reason.startsWith(WAITING_FOR_HUB_PREFIX)
	);
}

export function chipState(status: CloudSyncStatus | null): ChipState {
	if (status === null || !status.configured || !status.running) return 'off';
	if (status.update_required !== null) return 'update_required';
	if (isWaitingForHub(status)) return 'syncing';
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

/** Hover title CTA for the status chip trigger (not the Advanced options link). */
export const CHIP_QUICK_ACTIONS_CTA = 'Click for quick actions.';

export const ADVANCED_OPTIONS_LABEL = 'Advanced options';
export const SYNC_NOW_LABEL = 'Sync now';
export const REFRESH_STATUS_LABEL = 'Refresh status';

export function updateRequiredSummary(updateRequired: CloudSyncUpdateRequired): string {
	return (
		`App update required to sync: this machine speaks v${updateRequired.local_wire_version}, ` +
		`the hub speaks v${updateRequired.peer_wire_version}. ${updateRequired.action}.`
	);
}

export function updateRequiredHeadline(updateRequired: CloudSyncUpdateRequired): string {
	return (
		`App update required to sync: this machine speaks v${updateRequired.local_wire_version}, ` +
		`the hub speaks v${updateRequired.peer_wire_version}. Install the latest Open DJ on this ` +
		'machine (or upgrade the other side if this install is already current).'
	);
}

export function chipFullLabel(status: CloudSyncStatus | null): string {
	const state = chipState(status);
	if (state === 'update_required') return 'sync: update required';
	if (state === 'ok') return `sync: ok ${relativeTime(status?.last_push_at ?? null)}`;
	if (state === 'error') return 'sync: error';
	if (state === 'inconclusive')
		return `sync: inconclusive ${relativeTime(status?.last_push_at ?? null)}`;
	return `sync: ${state}`;
}

/** LESSV-03: the chip's glyphs for a status. Every state shows the cloud; a
 * working sync (ok) shows ONLY the cloud plus a tick, in white, and every other
 * state keeps its text label, because those are the ones that need reading. */
export function chipGlyphs(status: CloudSyncStatus | null): { cloud: true; tick: boolean; text: boolean } {
	const ok = chipState(status) === 'ok';
	return { cloud: true, tick: ok, text: !ok };
}

export function chipShortLabel(status: CloudSyncStatus | null): string {
	const state = chipState(status);
	if (state === 'off') return 'off';
	if (state === 'syncing') return 'sync';
	if (state === 'ok') return 'ok';
	if (state === 'update_required') return 'upd';
	if (state === 'error') return 'err';
	return 'inc';
}

// ----------------------------------------------------------- error presentation

export const CLOUDSYNC_LOCAL_LOCK_SUMMARY =
	'CloudSync conflict: another sync is already running. Wait for it to finish, then refresh status or try again.';
export const CLOUDSYNC_REMOTE_409_SUMMARY =
	'CloudSync conflict: the hub rejected this sync. Check the other device or hub, then try again when it is ready.';
export const CLOUDSYNC_GENERIC_ERROR_SUMMARY =
	'CloudSync sync failed. See technical details and try again.';
export const CLOUDSYNC_TECHNICAL_DETAILS_LABEL = 'Technical details';

export function presentUpdateRequired(
	updateRequired: CloudSyncUpdateRequired
): CloudSyncErrorPresentation {
	const summary = updateRequiredSummary(updateRequired);
	return { summary, ariaLabel: summary, details: summary, isConflict409: true };
}

export interface CloudSyncErrorPresentation {
	summary: string;
	ariaLabel: string;
	details: string;
	isConflict409: boolean;
}

function readApiErrorCode(error: unknown): string | null {
	if (
		typeof error === 'object' &&
		error !== null &&
		(error as { name?: string }).name === 'ApiError' &&
		typeof (error as { code?: unknown }).code === 'string'
	) {
		return (error as { code: string }).code;
	}
	return null;
}

function errorMessage(error: unknown): string {
	if (error instanceof Error) return error.message;
	return String(error);
}

function isLocalLockConflict(code: string | null, raw: string): boolean {
	return code === 'CLOUDSYNC_SYNC_IN_PROGRESS' || raw.includes('CLOUDSYNC_SYNC_IN_PROGRESS');
}

function isTransport409Conflict(raw: string): boolean {
	return raw.includes('HTTP 409');
}

function isDeclaredSyncRefusal(code: string | null, raw: string): boolean {
	return code === 'CLOUDSYNC_SYNC_REFUSED' || raw.includes('SyncDigestMismatch');
}

function buildErrorPresentation(
	raw: string,
	httpStatus: number | null,
	code: string | null
): CloudSyncErrorPresentation {
	const localLock = isLocalLockConflict(code, raw);
	const transport409 = isTransport409Conflict(raw);
	const declaredRefusal = isDeclaredSyncRefusal(code, raw);
	const isConflict409 = localLock || transport409 || declaredRefusal || httpStatus === 409;

	let summary: string;
	if (localLock) {
		summary = CLOUDSYNC_LOCAL_LOCK_SUMMARY;
	} else if (transport409 || declaredRefusal || httpStatus === 409) {
		summary = CLOUDSYNC_REMOTE_409_SUMMARY;
	} else {
		summary = CLOUDSYNC_GENERIC_ERROR_SUMMARY;
	}

	return { summary, ariaLabel: summary, details: raw, isConflict409 };
}

/** Plain-word summary for a thrown sync error; raw text stays in details only. */
export function presentCloudSyncError(error: unknown): CloudSyncErrorPresentation {
	return buildErrorPresentation(errorMessage(error), readApiErrorStatus(error), readApiErrorCode(error));
}

/** Plain-word summary for a journaled sync result row or chip last_result. */
export function presentCloudSyncResultError(
	result: {
		status: string;
		message: string;
	},
	updateRequired: CloudSyncUpdateRequired | null = null
): CloudSyncErrorPresentation {
	if (updateRequired !== null) {
		return presentUpdateRequired(updateRequired);
	}
	if (result.status !== 'error') {
		return {
			summary: result.message,
			ariaLabel: result.message,
			details: result.message,
			isConflict409: false
		};
	}
	return buildErrorPresentation(result.message, null, null);
}

export function chipTitle(status: CloudSyncStatus | null, loadError: string | null): string {
	if (status === null) {
		return loadError === null
			? `CloudSync status - still loading from the daemon. ${CHIP_QUICK_ACTIONS_CTA}`
			: `CloudSync status unavailable. ${CHIP_QUICK_ACTIONS_CTA}`;
	}
	const state = chipState(status);
	const next = CHIP_QUICK_ACTIONS_CTA;
	if (state === 'off') {
		return `CloudSync is off${status.reason ? ` (${status.reason})` : ''}. ${next}`;
	}
	if (state === 'update_required' && status.update_required !== null) {
		return `${updateRequiredSummary(status.update_required)} ${next}`;
	}
	if (state === 'error') {
		const summary =
			status.last_result !== null && status.last_result !== undefined
				? presentCloudSyncResultError(status.last_result).summary
				: CLOUDSYNC_GENERIC_ERROR_SUMMARY;
		return `${summary} ${next}`;
	}
	if (state === 'inconclusive') {
		return `CloudSync last run was inconclusive - agreement was not verified. ${next}`;
	}
	if (state === 'ok') {
		return `CloudSync last succeeded ${relativeTime(status.last_push_at)}. ${next}`;
	}
	return `CloudSync is syncing${status.rows_pending !== null ? ` (${status.rows_pending} rows pending)` : ''}. ${next}`;
}

export function chipAriaLabel(status: CloudSyncStatus | null, loadError: string | null): string {
	if (status === null) {
		return loadError === null ? 'CloudSync status' : 'CloudSync status unavailable';
	}
	if (chipState(status) === 'update_required' && status.update_required !== null) {
		return updateRequiredSummary(status.update_required);
	}
	if (chipState(status) === 'error') {
		const summary =
			status.last_result !== null && status.last_result !== undefined
				? presentCloudSyncResultError(status.last_result).summary
				: CLOUDSYNC_GENERIC_ERROR_SUMMARY;
		return summary;
	}
	return 'CloudSync status';
}

// ----------------------------------------------------------- sync now

export const SYNC_DEFER_GIG = 'gig_posture';
export const SYNC_DEFER_DECK_PLAYING = 'deck_playing';

export type UiMirrorDeck = { playing?: boolean };
export type UiMirrorDecks = Record<string, UiMirrorDeck | unknown>;

/**
 * ui-mirror read for Gig/playing-deck sync gates (HTTP parity with Status tab).
 * Fails closed: an UNEXPECTED fetch error propagates to the caller instead of
 * being read as "no deck playing", so a transient failure never widens the
 * sync gate. A 409 is the one EXPECTED shape here - `/api/v1/state/ui-mirror`
 * answers it whenever `/performance` is not the mounted route (every other
 * app-shell route, including `/cloudsync` itself), because there is no live
 * performance mirror to report. That is "no deck data", not a failure, and
 * must resolve to null rather than disable Sync / stall the Status tab on
 * every route besides `/performance`.
 */
export async function fetchUiMirrorForGate(): Promise<{ decks?: UiMirrorDecks } | null> {
	try {
		const { data } = await api.GET('/api/v1/state/ui-mirror', {});
		if (data !== undefined && typeof data === 'object') {
			return data as { decks?: UiMirrorDecks };
		}
		return null;
	} catch (exc) {
		if (readApiErrorStatus(exc) === 409) return null;
		throw exc;
	}
}

export function anyDeckPlaying(uiMirror: { decks?: UiMirrorDecks } | null): boolean {
	if (uiMirror === null) return false;
	const decks = uiMirror.decks;
	if (decks === undefined || typeof decks !== 'object') return false;
	for (const deck of Object.values(decks)) {
		if (typeof deck !== 'object' || deck === null) continue;
		if ((deck as UiMirrorDeck).playing === true) return true;
	}
	return false;
}

export function syncRuntimeGateReason(
	appPosture: 'prep' | 'gig',
	uiMirror: { decks?: UiMirrorDecks } | null
): typeof SYNC_DEFER_GIG | typeof SYNC_DEFER_DECK_PLAYING | null {
	if (appPosture === 'gig') return SYNC_DEFER_GIG;
	if (anyDeckPlaying(uiMirror)) return SYNC_DEFER_DECK_PLAYING;
	return null;
}

export const FORCE_SYNC_LABEL =
	"Force sync (oDJ won't sync during performance unless this is clicked)";

export type SyncNowDecision = { kind: 'post'; body: SyncRunBody } | { kind: 'refuse'; reason: string };

function syncHubUrlDecision(config: CloudSyncConfigOut | null): SyncNowDecision | { hubUrl: string } {
	if (config === null) {
		return { kind: 'refuse', reason: 'CloudSync config has not loaded yet.' };
	}
	const hubUrl = config.effective.hub_url;
	if (hubUrl === null) {
		return { kind: 'refuse', reason: 'Set a hub URL in the config form first.' };
	}
	return { hubUrl };
}

/** Sync now runs against the EFFECTIVE hub URL (env override included). */
export function syncNowRequest(
	config: CloudSyncConfigOut | null,
	gate: { appPosture: 'prep' | 'gig'; uiMirror: { decks?: UiMirrorDecks } | null }
): SyncNowDecision {
	const hub = syncHubUrlDecision(config);
	if ('kind' in hub) return hub;
	const deferReason = syncRuntimeGateReason(gate.appPosture, gate.uiMirror);
	if (deferReason !== null) {
		const label = deferReason === SYNC_DEFER_GIG ? 'Gig posture' : 'a playing deck';
		return {
			kind: 'refuse',
			reason: `CloudSync sync deferred (${deferReason}): ${label} is active. Use Force sync to bypass for one round.`
		};
	}
	return {
		kind: 'post',
		body: { hub_url: hub.hubUrl, name: config!.effective.machine_name, force: false }
	};
}

/** Force sync bypasses Gig and playing-deck gates for one round only. */
export function forceSyncNowRequest(config: CloudSyncConfigOut | null): SyncNowDecision {
	const hub = syncHubUrlDecision(config);
	if ('kind' in hub) return hub;
	return {
		kind: 'post',
		body: { hub_url: hub.hubUrl, name: config!.effective.machine_name, force: true }
	};
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

// ----------------------------------------------------------- status headline

export type HeadlineTone = 'off' | 'warn' | 'error' | 'ok';

export interface StatusHeadline {
	tone: HeadlineTone;
	text: string;
}

/**
 * Turns a raw sync-transport exception string into one plain clause a DJ can
 * act on. The raw message is NEVER dropped (fail-fast: no masking) -- callers
 * still show it verbatim behind a disclosure. This only prefixes it with a
 * cause instead of leaving a bare "error".
 */
export function plainSyncFailureCause(rawMessage: string): string {
	const msg = rawMessage.toLowerCase();
	if (msg.includes('connection refused')) {
		return 'could not reach the hub machine (connection refused)';
	}
	if (msg.includes('http 502')) {
		const elapsed = msg.match(/after (\d+(?:\.\d+)?)s/);
		if (elapsed !== null) {
			return `the hub or proxy closed the connection (502, after ${Math.round(Number(elapsed[1]))}s)`;
		}
		return 'the hub or proxy closed the connection (502; may be Tailscale serve or client timeout)';
	}
	if (msg.includes('client timeout after')) {
		const elapsed = msg.match(/client timeout after (\d+(?:\.\d+)?)s/);
		if (elapsed !== null) {
			return `the hub did not respond in time (timeout after ${Math.round(Number(elapsed[1]))}s)`;
		}
	}
	if (msg.includes('timed out') || msg.includes('timeout')) {
		return 'the hub did not respond in time (timeout)';
	}
	if (
		msg.includes('name or service not known') ||
		msg.includes('nodename nor servname') ||
		msg.includes('getaddrinfo')
	) {
		return 'the hub address could not be found (DNS lookup failed)';
	}
	if (msg.includes('401') || msg.includes('unauthorized') || msg.includes('403') || msg.includes('forbidden')) {
		return 'the hub rejected the sign-in';
	}
	return 'the last sync attempt failed';
}

/**
 * One plain-language status line with a concrete next step -- the "is my
 * library safe" headline the panel was missing (the maintainer, Mon 14 Sep 2026: "it
 * says error" with no explanation). The raw status fields (configured,
 * running, heartbeat, rows_pending) stay visible below for anyone who wants
 * them; this never replaces them, only leads with a sentence a DJ can read.
 */
export function statusHeadline(status: CloudSyncStatus | null): StatusHeadline {
	if (status === null) {
		return { tone: 'off', text: 'Loading CloudSync status...' };
	}
	// Current configuration wins over a historical journal verdict: the
	// journal keeps the last result even after the user turns automatic sync
	// off, so an old error must not outlive the config change that disabled
	// it (Devin review, PR #2604). A saved endpoint with automatic sync off
	// is also not "not set up" -- Sync now still works against it.
	if (!status.configured) {
		if (status.endpoint === null) {
			return {
				tone: 'off',
				text: 'CloudSync is not set up on this machine. Enter a hub URL below and save to turn it on.'
			};
		}
		return {
			tone: 'off',
			text: `Automatic sync is off, but a hub URL is saved (${status.endpoint}). Use Sync now below, or turn on automatic sync above.`
		};
	}
	if (status.update_required !== null) {
		return { tone: 'warn', text: updateRequiredHeadline(status.update_required) };
	}
	// A recorded result is checked BEFORE the heartbeat: what the last sync
	// actually did is more informative than whether the scheduler is alive
	// right now, and CSSTATUS-05 requires the "In sync" headline for every
	// recorded ok result even with a stale heartbeat (Sol review, PR #2604).
	if (status.last_result?.status === 'error') {
		if (
			status.digest_diff !== null &&
			status.digest_diff !== undefined &&
			status.digest_diff.length > 0
		) {
			const first = status.digest_diff[0];
			const sample = `${first.table}/${first.stable_id} (${first.newer_side} newer)`;
			return {
				tone: 'error',
				text: `Library metadata disagrees with the hub: ${sample}. See Technical detail for the full message.`
			};
		}
		if (status.credential_notice !== null && status.credential_notice !== undefined) {
			return {
				tone: 'warn',
				text: `${status.credential_notice.action}.`
			};
		}
		if (isWaitingForHub(status)) {
			return { tone: 'warn', text: `${status.reason} No action is needed on this machine.` };
		}
		return {
			tone: 'error',
			text: `Not synced: ${plainSyncFailureCause(status.last_result.message)}. Check the hub URL below and that the hub machine is running, then try Sync now again.`
		};
	}
	if (status.last_result?.status === 'ok') {
		return { tone: 'ok', text: `In sync. Last synced ${relativeTime(status.last_push_at)}.` };
	}
	if (status.last_result?.status === 'inconclusive') {
		return {
			tone: 'warn',
			text: 'Last sync finished but could not fully confirm both sides agree. Run Sync now again to reconfirm.'
		};
	}
	if (!status.running) {
		return {
			tone: 'warn',
			text: 'CloudSync is set up but not running automatically (no recent heartbeat). Use Sync now below, or start the background scheduler.'
		};
	}
	return { tone: 'warn', text: 'Syncing...' };
}

/**
 * Plain-language note for the identity-hold sync backlog (CLOUDSYNC-16,
 * Mon 14 Sep 2026: an 'inconclusive' result's only advice was
 * "run Sync now again", which does nothing when the true cause is rows that
 * can never sync until fingerprinted -- retrying re-runs the same exclusion
 * every time). Shown as a SEPARATE line from `statusHeadline`, never folded
 * into it: the backlog and any given sync's outcome are correlated on a
 * real library but not proven to be the same event on any one run, so this
 * never claims causation, only reports the count honestly.
 *
 * #3252: this used to print a `python -m ...` command for a person to run, sat
 * directly under a green "In sync" heading so background enrichment read as a
 * sync failure, and never said what was actually degraded. A human-facing panel
 * hands over no commands; it says what is affected and which machine can act.
 * The agent-facing path keeps the CLI, and the endpoint that would back a
 * button does not exist yet, which is tracked on #3252 rather than implied here.
 */
export function identityBacklogNote(hashPending: number | null): string | null {
	if (hashPending === null || hashPending <= 0) return null;
	const plural = hashPending !== 1;
	return (
		`${hashPending} track${plural ? 's' : ''} on the hub ${plural ? 'are' : 'is'} waiting for an ` +
		`audio fingerprint. Your library and your sync are unaffected: until ` +
		`${plural ? 'they are' : 'it is'} fingerprinted, duplicate detection and relinking moved ` +
		`files are less accurate for ${plural ? 'those tracks' : 'that track'}. Fingerprinting reads ` +
		`the audio file itself, so it runs on a machine that holds your music, never on the hub.`
	);
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
