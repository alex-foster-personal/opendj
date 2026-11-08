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
		presentCloudSyncError,
		statusHeadline,
		syncNowRequest,
		type UiMirrorGateDoc
	} from './cloudsync-view';
	import CloudSyncHeadlineStripView from './CloudSyncHeadlineStripView.svelte';

	interface Notice {
		kind: 'ok' | 'error';
		text: string;
		details?: string;
	}

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
			notice =
				statusLoadError === null && gateLoadError === null
					? { kind: 'ok', text: 'CloudSync status refreshed.' }
					: {
							kind: 'error',
							text: `Failed to refresh status: ${statusLoadError ?? gateLoadError ?? 'unknown error'}`
						};
			announceStatusChanged();
		} catch (exc) {
			notice = { kind: 'error', text: `Failed to refresh status: ${message(exc)}` };
		} finally {
			refreshing = false;
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
				text: `Sync ${result.digest_inconclusive ? 'inconclusive' : 'ok'}: pushed ${result.pushed}, pulled ${result.pulled}.`
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
