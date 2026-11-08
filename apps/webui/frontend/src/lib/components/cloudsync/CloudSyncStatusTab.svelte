<script lang="ts">
	/**
	 * /cloudsync Status tab: the truthful status panel, the per-machine config
	 * form, and a Sync now button. Owns the config and status it shows so the
	 * page stays wiring only. Outcomes render inline (role=status), readable
	 * by a person, an agent and the e2e alike. HTTP twins (agent parity):
	 *   GET  /api/v1/cloudsync/status   <- python -m apps.sync_hub status
	 *   GET/PUT /api/v1/cloudsync/config <- python -m apps.sync_hub config show|set
	 *   POST /api/v1/cloudsync/sync     <- python -m apps.sync_hub sync
	 */
	import { onMount } from 'svelte';

	import { getIdentityBacklog, getStatus, type CloudSyncStatus } from '$lib/api-cloudsync';
	import {
		getCloudSyncConfig,
		putCloudSyncConfig,
		runCloudSyncNow,
		type CloudSyncConfigOut
	} from '$lib/api-cloudsync-ops';
	import { uiPrefs } from '$lib/rb/prefs.svelte';

	import { warnForceSyncBypassesGate } from './cloudsync-toasts';
	import {
		CLOUDSYNC_TECHNICAL_DETAILS_LABEL,
		FORCE_SYNC_LABEL,
		STATUS_CHANGED_EVENT,
		configPutBody,
		envOverrideNotes,
		fetchUiMirrorForGate,
		forceSyncNowRequest,
		formFromConfig,
		identityBacklogNote,
		presentCloudSyncError,
		presentCloudSyncResultError,
		syncNowRequest,
		syncRuntimeGateReason,
		type ConfigFormFields,
		type UiMirrorGateDoc
	} from './cloudsync-view';
	import CloudSyncStatusTabView from './CloudSyncStatusTabView.svelte';

	interface Notice {
		kind: 'ok' | 'error';
		text: string;
		/** Hover explanation, required whenever the text carries counts. */
		title?: string;
		/** Raw error text for the technical-details disclosure only. */
		details?: string;
	}

	/** Tell the status chip to re-read now instead of on its next poll. */
	function announceStatusChanged(): void {
		window.dispatchEvent(new CustomEvent(STATUS_CHANGED_EVENT));
	}

	let status = $state<CloudSyncStatus | null>(null);
	/** null = not yet loaded (or failed to load); the note stays hidden either way. */
	let identityBacklogCount = $state<number | null>(null);
	let config = $state<CloudSyncConfigOut | null>(null);
	let loadError = $state<string | null>(null);
	let form = $state<ConfigFormFields>({ enabled: false, hubUrl: '', machineName: '' });
	let saving = $state(false);
	let syncing = $state(false);
	let notice = $state<Notice | null>(null);
	let uiMirror = $state<UiMirrorGateDoc | null>(null);

	const syncGate = $derived({
		appPosture: uiPrefs.app_posture,
		uiMirror
	});
	const syncDecision = $derived(syncNowRequest(config, syncGate));
	const forceSyncDecision = $derived(forceSyncNowRequest(config));
	const showForceSync = $derived(
		config?.effective.hub_url !== null &&
			config?.effective.hub_url !== undefined &&
			syncRuntimeGateReason(syncGate.appPosture, syncGate.uiMirror) !== null
	);

	function message(exc: unknown): string {
		return exc instanceof Error ? exc.message : String(exc);
	}

	/**
	 * Best-effort: the identity backlog is a supplementary telemetry note, not
	 * core status, so a failure here must never block the status panel from
	 * rendering (same reasoning as `fetchUiMirrorForGate`).
	 */
	async function fetchIdentityBacklog(): Promise<number | null> {
		try {
			return (await getIdentityBacklog()).hash_pending;
		} catch {
			return null;
		}
	}

	async function refresh(): Promise<void> {
		try {
			const [nextStatus, nextConfig, nextMirror, nextBacklog] = await Promise.all([
				getStatus(),
				getCloudSyncConfig(),
				fetchUiMirrorForGate(),
				fetchIdentityBacklog()
			]);
			status = nextStatus;
			config = nextConfig;
			uiMirror = nextMirror;
			identityBacklogCount = nextBacklog;
			loadError = null;
		} catch (exc) {
			loadError = message(exc);
		}
	}

	onMount(async () => {
		await refresh();
		if (config !== null) form = formFromConfig(config);
	});

	async function saveConfig(): Promise<void> {
		const decision = configPutBody(form);
		if (decision.kind === 'refuse') {
			notice = { kind: 'error', text: decision.reason };
			return;
		}
		saving = true;
		try {
			config = await putCloudSyncConfig(decision.body);
			form = formFromConfig(config);
			notice = { kind: 'ok', text: 'CloudSync config saved.' };
			status = await getStatus();
			announceStatusChanged();
		} catch (exc) {
			notice = { kind: 'error', text: `Failed to save CloudSync config: ${message(exc)}` };
		} finally {
			saving = false;
		}
	}

	async function syncNow(): Promise<void> {
		if (syncDecision.kind === 'refuse') {
			notice = { kind: 'error', text: syncDecision.reason };
			return;
		}
		syncing = true;
		try {
			const result = await runCloudSyncNow(syncDecision.body);
			notice = {
				kind: 'ok',
				text: `Sync ${result.digest_inconclusive ? 'inconclusive' : 'ok'}: pushed ${result.pushed}, pulled ${result.pulled}.`,
				title: `Rows this Sync now pushed to the hub (${result.pushed}) and pulled from it (${result.pulled}); ${result.rounds} round(s), hub sequence ${result.hub_seq}`
			};
		} catch (exc) {
			// The failure was journaled server-side; the refresh below shows it.
			const presented = presentCloudSyncError(exc);
			notice = {
				kind: 'error',
				text: `Sync failed: ${presented.summary}`,
				details: presented.details
			};
		} finally {
			syncing = false;
			await refresh();
			announceStatusChanged();
		}
	}

	async function forceSyncNow(): Promise<void> {
		if (forceSyncDecision.kind === 'refuse') {
			notice = { kind: 'error', text: forceSyncDecision.reason };
			return;
		}
		warnForceSyncBypassesGate();
		syncing = true;
		try {
			const result = await runCloudSyncNow(forceSyncDecision.body);
			notice = {
				kind: 'ok',
				text: `Sync ${result.digest_inconclusive ? 'inconclusive' : 'ok'}: pushed ${result.pushed}, pulled ${result.pulled}.`,
				title: `Rows this Force sync pushed to the hub (${result.pushed}) and pulled from it (${result.pulled}); ${result.rounds} round(s), hub sequence ${result.hub_seq}`
			};
		} catch (exc) {
			const presented = presentCloudSyncError(exc);
			notice = {
				kind: 'error',
				text: `Sync failed: ${presented.summary}`,
				details: presented.details
			};
		} finally {
			syncing = false;
			await refresh();
			announceStatusChanged();
		}
	}

	const backlogNote = $derived(identityBacklogNote(identityBacklogCount));
</script>

<CloudSyncStatusTabView
	{status}
	{config}
	{loadError}
	{notice}
	bind:form
	{saving}
	{syncing}
	{syncDecision}
	{forceSyncDecision}
	{showForceSync}
	{backlogNote}
	technicalDetailsLabel={CLOUDSYNC_TECHNICAL_DETAILS_LABEL}
	forceSyncLabel={FORCE_SYNC_LABEL}
	{presentCloudSyncResultError}
	{envOverrideNotes}
	{syncNow}
	{forceSyncNow}
	{refresh}
	{saveConfig}
/>
