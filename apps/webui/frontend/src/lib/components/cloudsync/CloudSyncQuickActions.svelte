<script lang="ts">
	/**
	 * In-place CloudSync quick actions for the status chip (issue #3531).
	 * HTTP twins: GET status/config/ui-mirror, POST /api/v1/cloudsync/sync.
	 */
	import { onMount } from 'svelte';
	import { getStatus } from '$lib/api-cloudsync';
	import {
		getCloudSyncConfig,
		runCloudSyncNow,
		type CloudSyncConfigOut
	} from '$lib/api-cloudsync-ops';
	import { uiPrefs } from '$lib/rb/prefs.svelte';

	import { warnForceSyncBypassesGate } from './cloudsync-toasts';
	import {
		ADVANCED_OPTIONS_LABEL,
		CHIP_HREF,
		FORCE_SYNC_LABEL,
		REFRESH_STATUS_LABEL,
		STATUS_CHANGED_EVENT,
		SYNC_NOW_LABEL,
		fetchUiMirrorForGate,
		forceSyncNowRequest,
		refreshStatusNotice,
		syncFailureNotice,
		syncNowRequest,
		syncRefusedNotice,
		syncResultNotice,
		syncRuntimeGateReason,
		type CloudSyncNotice,
		type UiMirrorGateDoc
	} from './cloudsync-view';
	import CloudSyncQuickActionsView from './CloudSyncQuickActionsView.svelte';

	type Notice = CloudSyncNotice;

	let {
		getTrigger,
		onclose,
		popoverEl = $bindable(null)
	}: {
		getTrigger: () => HTMLElement | null;
		onclose: () => void;
		popoverEl?: HTMLDivElement | null;
	} = $props();

	let config = $state<CloudSyncConfigOut | null>(null);
	let uiMirror = $state<UiMirrorGateDoc | null>(null);
	let loadError = $state<string | null>(null);
	let syncing = $state(false);
	let refreshing = $state(false);
	let notice = $state<Notice | null>(null);

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

	function announceStatusChanged(): void {
		window.dispatchEvent(new CustomEvent(STATUS_CHANGED_EVENT));
	}

	function message(exc: unknown): string {
		return exc instanceof Error ? exc.message : String(exc);
	}

	async function loadGateData(): Promise<void> {
		try {
			const [nextConfig, nextMirror] = await Promise.all([
				getCloudSyncConfig(),
				fetchUiMirrorForGate()
			]);
			config = nextConfig;
			uiMirror = nextMirror;
			loadError = null;
		} catch (exc) {
			loadError = message(exc);
		}
	}

	onMount(() => {
		void loadGateData();
	});

	async function refreshStatus(): Promise<void> {
		refreshing = true;
		try {
			await getStatus();
			await loadGateData();
			// loadGateData catches its own errors (into loadError) so a failed
			// gate reload does not throw here - check it explicitly, or a failed
			// reload still reports "refreshed" next to its own error alert.
			notice = refreshStatusNotice(loadError);
			announceStatusChanged();
		} catch (exc) {
			notice = { kind: 'error', text: `Failed to refresh status: ${message(exc)}` };
		} finally {
			refreshing = false;
		}
	}

	async function syncNow(): Promise<void> {
		if (syncDecision.kind === 'refuse') {
			notice = syncRefusedNotice(syncDecision);
			return;
		}
		syncing = true;
		try {
			notice = syncResultNotice(await runCloudSyncNow(syncDecision.body));
		} catch (exc) {
			notice = syncFailureNotice(exc);
		} finally {
			syncing = false;
			await loadGateData();
			announceStatusChanged();
		}
	}

	async function forceSyncNow(): Promise<void> {
		if (forceSyncDecision.kind === 'refuse') {
			notice = syncRefusedNotice(forceSyncDecision);
			return;
		}
		warnForceSyncBypassesGate();
		syncing = true;
		try {
			notice = syncResultNotice(await runCloudSyncNow(forceSyncDecision.body));
		} catch (exc) {
			notice = syncFailureNotice(exc);
		} finally {
			syncing = false;
			await loadGateData();
			announceStatusChanged();
		}
	}
</script>

<CloudSyncQuickActionsView
	{getTrigger}
	{onclose}
	bind:popoverEl
	{syncing}
	{refreshing}
	{loadError}
	{notice}
	{syncDecision}
	{forceSyncDecision}
	{showForceSync}
	syncNowLabel={SYNC_NOW_LABEL}
	forceSyncLabel={FORCE_SYNC_LABEL}
	refreshStatusLabel={REFRESH_STATUS_LABEL}
	advancedLabel={ADVANCED_OPTIONS_LABEL}
	advancedHref={CHIP_HREF}
	onSyncNow={() => void syncNow()}
	onForceSyncNow={() => void forceSyncNow()}
	onRefreshStatus={() => void refreshStatus()}
/>
