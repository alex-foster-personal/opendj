<script lang="ts">
	/**
	 * Shared CloudSync headline strip above every /cloudsync tab (issue #8720).
	 * HTTP twins: GET status/config/ui-mirror, POST /api/v1/cloudsync/sync.
	 */
	import { onMount } from 'svelte';
	import { getStatus, type CloudSyncStatus } from '$lib/api-cloudsync';
	import {
		getCloudSyncConfig,
		runCloudSyncNow,
		type CloudSyncConfigOut
	} from '$lib/api-cloudsync-ops';
	import { uiPrefs } from '$lib/rb/prefs.svelte';
	import { coalesce } from '$lib/rb/coalesce';

	import {
		CHIP_POLL_MS,
		HEADLINE_REFRESH_LABEL,
		SET_UP_LABEL,
		STATUS_CHANGED_EVENT,
		SYNC_NOW_LABEL,
		fetchUiMirrorForGate,
		headlineStripAction,
		refreshStatusNotice,
		statusHeadline,
		syncFailureNotice,
		syncNowRequest,
		syncRefusedNotice,
		syncResultNotice,
		type CloudSyncNotice,
		type UiMirrorGateDoc
	} from './cloudsync-view';
	import CloudSyncHeadlineStripView from './CloudSyncHeadlineStripView.svelte';

	type Notice = CloudSyncNotice;

	let { onSelectStatusTab }: { onSelectStatusTab: () => void } = $props();

	let status = $state<CloudSyncStatus | null>(null);
	let config = $state<CloudSyncConfigOut | null>(null);
	let uiMirror = $state<UiMirrorGateDoc | null>(null);
	let statusLoadError = $state<string | null>(null);
	let gateLoadError = $state<string | null>(null);
	let syncing = $state(false);
	let refreshing = $state(false);
	let notice = $state<Notice | null>(null);

	const syncGate = $derived({
		appPosture: uiPrefs.app_posture,
		uiMirror
	});
	const syncDecision = $derived(syncNowRequest(config, syncGate));
	const headline = $derived(
		statusLoadError !== null && status === null
			? { tone: 'error' as const, text: `Could not load CloudSync status. ${statusLoadError}` }
			: statusHeadline(status)
	);
	const action = $derived(headlineStripAction(status));

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
			gateLoadError = null;
		} catch (exc) {
			gateLoadError = message(exc);
		}
	}

	async function loadStatus(): Promise<void> {
		try {
			status = await getStatus();
			statusLoadError = null;
		} catch (exc) {
			statusLoadError = message(exc);
		}
	}

	async function loadAll(): Promise<void> {
		await Promise.all([loadStatus(), loadGateData()]);
	}

	const loadOnce = coalesce(loadAll);

	async function refreshStatus(): Promise<void> {
		refreshing = true;
		try {
			await loadAll();
			notice = refreshStatusNotice(statusLoadError ?? gateLoadError);
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
			await loadAll();
			announceStatusChanged();
		}
	}

	onMount(() => {
		void loadOnce();
		const timer = setInterval(() => {
			if (!document.hidden) void loadOnce();
		}, CHIP_POLL_MS);
		const onStatusChanged = (): void => void loadOnce();
		window.addEventListener(STATUS_CHANGED_EVENT, onStatusChanged);
		return () => {
			clearInterval(timer);
			window.removeEventListener(STATUS_CHANGED_EVENT, onStatusChanged);
		};
	});
</script>

<CloudSyncHeadlineStripView
	{headline}
	{action}
	syncNowLabel={SYNC_NOW_LABEL}
	refreshLabel={HEADLINE_REFRESH_LABEL}
	setUpLabel={SET_UP_LABEL}
	{syncing}
	{refreshing}
	{gateLoadError}
	{syncDecision}
	{notice}
	onSyncNow={() => void syncNow()}
	onRefresh={() => void refreshStatus()}
	onSetUp={() => onSelectStatusTab()}
/>
