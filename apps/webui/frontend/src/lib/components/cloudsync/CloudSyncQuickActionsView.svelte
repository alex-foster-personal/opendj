<script lang="ts">
	/**
	 * Props-only half of CloudSyncQuickActions: the chip's in-place quick
	 * actions popover (issue #3531). The container loads the gate data, makes
	 * every request and holds the notice; this file draws the buttons from
	 * the decisions it is handed. Labels arrive as props because
	 * cloudsync-view.ts reaches the backend; only its types are imported.
	 */
	import { triggerFloatingAction } from '$lib/ui/clamp-to-viewport';
	import type { SyncNowDecision } from './cloudsync-view';

	interface Notice {
		kind: 'ok' | 'error';
		text: string;
		details?: string;
	}

	let {
		getTrigger,
		onclose,
		popoverEl = $bindable(null),
		syncing,
		refreshing,
		loadError,
		notice,
		syncDecision,
		forceSyncDecision,
		showForceSync,
		syncNowLabel,
		forceSyncLabel,
		refreshStatusLabel,
		advancedLabel,
		advancedHref,
		onSyncNow,
		onForceSyncNow,
		onRefreshStatus
	}: {
		getTrigger: () => HTMLElement | null;
		onclose: () => void;
		popoverEl?: HTMLDivElement | null;
		syncing: boolean;
		refreshing: boolean;
		loadError: string | null;
		notice: Notice | null;
		syncDecision: SyncNowDecision;
		forceSyncDecision: SyncNowDecision;
		showForceSync: boolean;
		syncNowLabel: string;
		forceSyncLabel: string;
		refreshStatusLabel: string;
		advancedLabel: string;
		advancedHref: string;
		onSyncNow: () => void;
		onForceSyncNow: () => void;
		onRefreshStatus: () => void;
	} = $props();
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
			disabled={syncing || refreshing || loadError !== null || syncDecision.kind === 'refuse'}
			title={loadError !== null
				? `Gate data failed to load: ${loadError}`
				: syncDecision.kind === 'refuse'
					? syncDecision.reason
					: 'Run one push and pull now (POST /api/v1/cloudsync/sync)'}
			onclick={() => onSyncNow()}
		>
			{syncing ? 'Syncing...' : syncNowLabel}
		</button>
		{#if showForceSync}
			<button
				type="button"
				data-testid="cloudsync-quick-force-sync"
				disabled={syncing || refreshing || loadError !== null || forceSyncDecision.kind === 'refuse'}
				title={loadError !== null
					? `Gate data failed to load: ${loadError}`
					: `${forceSyncLabel} (POST /api/v1/cloudsync/sync with force=true)`}
				onclick={() => onForceSyncNow()}
			>
				{syncing ? 'Syncing...' : forceSyncLabel}
			</button>
		{/if}
		<button
			type="button"
			data-testid="cloudsync-quick-refresh"
			disabled={syncing || refreshing}
			title="Re-read GET /api/v1/cloudsync/status and config"
			onclick={() => onRefreshStatus()}
		>
			{refreshing ? 'Refreshing...' : refreshStatusLabel}
		</button>
	</div>
	<a
		class="advanced-link"
		data-testid="cloudsync-quick-advanced"
		href={advancedHref}
		onclick={() => onclose()}
	>
		{advancedLabel}
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
