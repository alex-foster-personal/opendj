<script lang="ts">
	/**
	 * In-place CloudSync quick actions for the status chip (issue #3531).
	 * HTTP twins: GET status/config/ui-mirror, POST /api/v1/cloudsync/sync.
	 */
	import { onMount } from 'svelte';
	import { triggerFloatingAction } from '$lib/ui/clamp-to-viewport';
	import { getStatus } from '$lib/api-cloudsync';
	import {
		getCloudSyncConfig,
		runCloudSyncNow,
		type CloudSyncConfigOut
	} from '$lib/api-cloudsync-ops';
	import { uiPrefs } from '$lib/rb/prefs.svelte';
	import { pushToast } from '$lib/stores.svelte';

	import {
		ADVANCED_OPTIONS_LABEL,
		CHIP_HREF,
		FORCE_SYNC_LABEL,
		REFRESH_STATUS_LABEL,
		STATUS_CHANGED_EVENT,
		SYNC_NOW_LABEL,
		fetchUiMirrorForGate,
		forceSyncNowRequest,
		presentCloudSyncError,
		syncNowRequest,
		syncRuntimeGateReason,
		type UiMirrorDecks
	} from './cloudsync-view';

	interface Notice {
		kind: 'ok' | 'error';
		text: string;
		details?: string;
	}

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
	let uiMirror = $state<{ decks?: UiMirrorDecks } | null>(null);
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
			notice = { kind: 'ok', text: 'CloudSync status refreshed.' };
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
			await loadGateData();
			announceStatusChanged();
		}
	}

	async function forceSyncNow(): Promise<void> {
		if (forceSyncDecision.kind === 'refuse') {
			notice = { kind: 'error', text: forceSyncDecision.reason };
			return;
		}
		pushToast(
			'Force sync bypasses Gig and playing-deck protection for one round.',
			'warn'
		);
		syncing = true;
		try {
			const result = await runCloudSyncNow(forceSyncDecision.body);
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
			await loadGateData();
			announceStatusChanged();
		}
	}
</script>

<div
	bind:this={popoverEl}
	class="cloudsync-quick-actions"
	data-testid="cloudsync-quick-actions-popover"
	role="dialog"
	aria-label="CloudSync quick actions"
	use:triggerFloatingAction={{ getTrigger, preferred: 'below', gap: 4 }}
>
	<div class="actions">
		<button
			type="button"
			data-testid="cloudsync-quick-sync-now"
			disabled={syncing || refreshing || syncDecision.kind === 'refuse'}
			title={syncDecision.kind === 'refuse'
				? syncDecision.reason
				: 'Run one push and pull now (POST /api/v1/cloudsync/sync)'}
			onclick={() => void syncNow()}
		>
			{syncing ? 'Syncing...' : SYNC_NOW_LABEL}
		</button>
		{#if showForceSync}
			<button
				type="button"
				data-testid="cloudsync-quick-force-sync"
				disabled={syncing || refreshing || forceSyncDecision.kind === 'refuse'}
				title={`${FORCE_SYNC_LABEL} (POST /api/v1/cloudsync/sync with force=true)`}
				onclick={() => void forceSyncNow()}
			>
				{syncing ? 'Syncing...' : FORCE_SYNC_LABEL}
			</button>
		{/if}
		<button
			type="button"
			data-testid="cloudsync-quick-refresh"
			disabled={syncing || refreshing}
			title="Re-read GET /api/v1/cloudsync/status and config"
			onclick={() => void refreshStatus()}
		>
			{refreshing ? 'Refreshing...' : REFRESH_STATUS_LABEL}
		</button>
	</div>
	<a
		class="advanced-link"
		data-testid="cloudsync-quick-advanced"
		href={CHIP_HREF}
		onclick={() => onclose()}
	>
		{ADVANCED_OPTIONS_LABEL}
	</a>
	{#if loadError !== null}
		<p class="err" role="alert">{loadError}</p>
	{/if}
	{#if notice !== null}
		<div class="notice" class:err={notice.kind === 'error'} class:ok={notice.kind === 'ok'} role="status">
			<p>{notice.text}</p>
			{#if notice.details !== undefined}
				<details data-testid="cloudsync-quick-notice-details">
					<summary>Technical details</summary>
					<pre class="technical-details">{notice.details}</pre>
				</details>
			{/if}
		</div>
	{/if}
</div>

<style>
	.cloudsync-quick-actions {
		position: fixed;
		z-index: 9600;
		min-width: 220px;
		max-width: min(360px, calc(100vw - 16px));
		padding: 0.5rem;
		border: 1px solid var(--muted);
		border-radius: 6px;
		background: var(--panel, #14171d);
		color: var(--text, #e8eaed);
		box-shadow: 0 4px 16px rgb(0 0 0 / 35%);
	}

	.actions {
		display: flex;
		flex-direction: column;
		gap: 0.35rem;
	}

	.actions button {
		width: 100%;
		text-align: left;
		font: inherit;
		font-size: 0.8rem;
		padding: 0.35rem 0.5rem;
		border: 1px solid var(--muted);
		border-radius: 4px;
		background: transparent;
		color: inherit;
		cursor: pointer;
	}

	.actions button:disabled {
		opacity: 0.55;
		cursor: not-allowed;
	}

	.advanced-link {
		display: block;
		margin-top: 0.45rem;
		font-size: 0.78rem;
		color: var(--accent, #3d7dd9);
		text-decoration: none;
	}

	.advanced-link:hover {
		text-decoration: underline;
	}

	.err {
		margin: 0.35rem 0 0;
		color: var(--danger, #e5534b);
		font-size: 0.75rem;
	}

	.notice {
		margin-top: 0.35rem;
		font-size: 0.75rem;
	}

	.notice.err {
		color: var(--danger, #e5534b);
	}

	.notice.ok {
		color: var(--accent-dim, #7eb8ff);
	}

	.notice p {
		margin: 0;
	}

	.technical-details {
		margin: 0.25rem 0 0;
		white-space: pre-wrap;
		word-break: break-word;
		font-size: 0.7rem;
	}
</style>
