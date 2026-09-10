<script lang="ts">
	import { onMount } from 'svelte';
	import { getStatus, type CloudSyncStatus } from '$lib/api-cloudsync';

	let status = $state<CloudSyncStatus | null>(null);
	let detailsOpen = $state(false);
	let loadError = $state<string | null>(null);

	function chipState(): 'off' | 'syncing' | 'ok' | 'error' | 'inconclusive' {
		if (status === null || !status.enabled) return 'off';
		if (status.last_result?.status === 'error') return 'error';
		// Its own state, never folded into 'ok': the sync completed but its
		// digest compare excluded rows, so agreement was not verified.
		if (status.last_result?.status === 'inconclusive') return 'inconclusive';
		if (status.last_result?.status === 'ok') return 'ok';
		return 'syncing';
	}

	function relativeTime(value: string | null): string {
		if (value === null) return 'never';
		const delta = Date.now() - Date.parse(value);
		if (!Number.isFinite(delta) || delta < 0) return value;
		const minutes = Math.floor(delta / 60_000);
		if (minutes < 1) return 'just now';
		if (minutes < 60) return `${minutes}m ago`;
		return `${Math.floor(minutes / 60)}h ago`;
	}

	const label = $derived.by(() => {
		const state = chipState();
		if (state === 'ok') return `sync: ok ${relativeTime(status?.last_push_at ?? null)}`;
		if (state === 'error') return 'sync: error';
		if (state === 'inconclusive')
			return `sync: inconclusive ${relativeTime(status?.last_push_at ?? null)}`;
		return `sync: ${state}`;
	});
	const title = $derived.by(() => {
		if (status === null) {
			return loadError === null
				? 'CloudSync status - still loading from the daemon. Click after it loads to see details.'
				: `CloudSync status unavailable: ${loadError}. Click to retry details.`;
		}
		const state = chipState();
		const next = 'Click to open recent results.';
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
	});

	onMount(() => {
		void getStatus()
			.then((next) => {
				status = next;
				loadError = null;
			})
			.catch((error: unknown) => {
				loadError = error instanceof Error ? error.message : String(error);
			});
	});
</script>

<div class="cloudsync-status">
	<button
		type="button"
		class:error={chipState() === 'error'}
		class:ok={chipState() === 'ok'}
		class:inconclusive={chipState() === 'inconclusive'}
		class="chip"
		title={title}
		aria-expanded={detailsOpen}
		aria-label="CloudSync status"
		onclick={() => (detailsOpen = !detailsOpen)}
	>
		{label}
	</button>
	{#if detailsOpen}
		<div class="details" role="status">
			<strong>CloudSync</strong>
			<p>{status?.reason ?? status?.endpoint ?? loadError ?? 'Checking status.'}</p>
			{#if status?.recent_results.length}
				<ol>
					{#each status.recent_results as result}
						<li>{result.finished_at}: {result.status} - {result.message}</li>
					{/each}
				</ol>
			{:else}
				<p>No sync attempts have completed yet.</p>
			{/if}
		</div>
	{/if}
</div>

<style>
	.cloudsync-status { position: relative; }
	.chip { border: 1px solid var(--muted); border-radius: 999px; background: transparent; color: var(--muted); font: inherit; font-size: 0.72rem; padding: 0.18rem 0.45rem; cursor: pointer; }
	.chip.ok { border-color: var(--accent-dim); color: var(--accent); }
	.chip.error { border-color: var(--danger); color: var(--danger); }
	.chip.inconclusive { border-color: var(--warning, #b8860b); color: var(--warning, #b8860b); }
	.details { position: absolute; z-index: 40; right: 0; top: calc(100% + 6px); width: 320px; padding: 0.6rem; border: 1px solid var(--border); border-radius: 6px; background: var(--surface); color: var(--fg); font-size: 0.75rem; }
	.details p { margin: 0.35rem 0; }
	.details ol { max-height: 12rem; overflow: auto; margin: 0; padding-left: 1.2rem; }
</style>
