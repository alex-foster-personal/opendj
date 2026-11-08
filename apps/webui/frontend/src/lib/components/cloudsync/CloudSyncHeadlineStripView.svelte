<script lang="ts">
	/**
	 * Props-only half of CloudSyncHeadlineStrip: one headline sentence plus a
	 * single mockup action above every /cloudsync tab (issue #8720).
	 */
	import type { HeadlineStripAction, StatusHeadline, SyncNowDecision } from './cloudsync-view';

	interface Notice {
		kind: 'ok' | 'error';
		text: string;
		details?: string;
	}

	let {
		headline,
		action,
		syncNowLabel,
		refreshLabel,
		setUpLabel,
		syncing,
		refreshing,
		gateLoadError,
		syncDecision,
		notice,
		onSyncNow,
		onRefresh,
		onSetUp
	}: {
		headline: StatusHeadline;
		action: HeadlineStripAction;
		syncNowLabel: string;
		refreshLabel: string;
		setUpLabel: string;
		syncing: boolean;
		refreshing: boolean;
		gateLoadError: string | null;
		syncDecision: SyncNowDecision;
		notice: Notice | null;
		onSyncNow: () => void;
		onRefresh: () => void;
		onSetUp: () => void;
	} = $props();
</script>

<div class="cloudsync-headline-strip" data-testid="cloudsync-headline-strip">
	<p
		class="headline"
		class:err={headline.tone === 'error'}
		class:warn={headline.tone === 'warn'}
		class:ok={headline.tone === 'ok'}
		data-testid="cloudsync-headline-text"
	>
		{headline.text}
	</p>
	<div class="strip-actions">
		{#if action === 'sync-now'}
			<button
				type="button"
				data-testid="cloudsync-headline-sync-now"
				disabled={syncing || refreshing || gateLoadError !== null || syncDecision.kind === 'refuse'}
				title={gateLoadError !== null
					? `Gate data failed to load: ${gateLoadError}`
					: syncDecision.kind === 'refuse'
						? syncDecision.reason
						: 'Run one push and pull now (POST /api/v1/cloudsync/sync)'}
				onclick={() => onSyncNow()}
			>
				{syncing ? 'Syncing...' : syncNowLabel}
			</button>
		{:else if action === 'refresh'}
			<button
				type="button"
				data-testid="cloudsync-headline-refresh"
				disabled={syncing || refreshing}
				title="Re-read GET /api/v1/cloudsync/status and config"
				onclick={() => onRefresh()}
			>
				{refreshing ? 'Refreshing...' : refreshLabel}
			</button>
		{:else}
			<button
				type="button"
				data-testid="cloudsync-headline-set-up"
				disabled={syncing || refreshing}
				title="Open Status & config to enter a hub URL"
				onclick={() => onSetUp()}
			>
				{setUpLabel}
			</button>
		{/if}
	</div>
</div>
{#if notice !== null}
	<div
		class="strip-notice"
		class:err={notice.kind === 'error'}
		class:ok={notice.kind === 'ok'}
		role="status"
	>
		<p>{notice.text}</p>
		{#if notice.details !== undefined}
			<details data-testid="cloudsync-headline-notice-details">
				<summary>Technical details</summary>
				<pre class="technical-details">{notice.details}</pre>
			</details>
		{/if}
	</div>
{/if}

<style>
	.cloudsync-headline-strip {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		justify-content: space-between;
		gap: 0.75rem;
		margin: 0.75rem 0 1rem;
		padding: 0.65rem 0.75rem;
		border: 1px solid var(--muted);
		border-radius: 6px;
		background: var(--panel, #14171d);
	}

	.headline {
		margin: 0;
		flex: 1 1 12rem;
		font-size: 0.9rem;
		line-height: 1.35;
	}

	.headline.ok {
		color: var(--accent-dim, #7eb8ff);
	}

	.headline.warn {
		color: var(--warn, #e6a817);
	}

	.headline.err {
		color: var(--danger, #e5534b);
	}

	.strip-actions button {
		font: inherit;
		font-size: 0.85rem;
		padding: 0.35rem 0.65rem;
		border: 1px solid var(--muted);
		border-radius: 4px;
		background: transparent;
		color: inherit;
		cursor: pointer;
		white-space: nowrap;
	}

	.strip-actions button:disabled {
		opacity: 0.55;
		cursor: not-allowed;
	}

	.strip-notice {
		margin: -0.35rem 0 0.75rem;
		font-size: 0.78rem;
	}

	.strip-notice.err {
		color: var(--danger, #e5534b);
	}

	.strip-notice.ok {
		color: var(--accent-dim, #7eb8ff);
	}

	.strip-notice p {
		margin: 0;
	}

	.technical-details {
		margin: 0.25rem 0 0;
		white-space: pre-wrap;
		word-break: break-word;
		font-size: 0.7rem;
	}
</style>
