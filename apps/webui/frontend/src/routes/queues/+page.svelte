<script lang="ts">
	import { onMount } from 'svelte';
	import { getQueue, type QueueOut } from '$lib/api';
	import { describeLoadError } from '$lib/route-load-state';

	let tab = $state<'dedup' | 'bad_beatgrid' | 'auto_cue'>('dedup');
	let queue = $state<QueueOut | null>(null);
	let loading = $state(true);
	let loadError = $state<string | null>(null);
	// A tab switch while a load is in flight must not let the older answer win.
	let loadSeq = 0;

	async function load(): Promise<void> {
		const seq = ++loadSeq;
		loading = true;
		loadError = null;
		try {
			const next = await getQueue(tab);
			if (seq !== loadSeq) return;
			queue = next;
		} catch (exc) {
			if (seq !== loadSeq) return;
			queue = null;
			loadError = describeLoadError(exc);
		} finally {
			if (seq === loadSeq) loading = false;
		}
	}

	function change(kind: typeof tab): void {
		tab = kind;
		queue = null;
		void load();
	}

	onMount(() => {
		void load();
	});
</script>

<h2>Triage queues (read-only)</h2>
<div class="tabs">
	<button onclick={() => change('dedup')} class:primary={tab === 'dedup'}>Dedup</button>
	<button onclick={() => change('bad_beatgrid')} class:primary={tab === 'bad_beatgrid'}>Bad beatgrid</button>
	<button onclick={() => change('auto_cue')} class:primary={tab === 'auto_cue'}>Auto cue</button>
</div>

{#if loadError !== null}
	<div class="load-error" role="alert">
		<p>Could not load this queue: {loadError}</p>
		<button type="button" onclick={() => void load()}>Retry</button>
	</div>
{:else if loading || queue === null}
	<p class="muted spaced">Loading queue...</p>
{:else}
	{#if queue.note}
		<p class="muted note">{queue.note}</p>
	{/if}
	{#if queue.items.length === 0}
		<p class="muted spaced">This queue is empty: nothing needs triage here right now.</p>
	{:else}
		<table class="library spaced">
			<thead><tr><th>Stable id</th><th>Payload</th></tr></thead>
			<tbody>
				{#each queue.items as item}
					<tr>
						<td><a href={`/track/${item.stable_id}`}>{item.stable_id}</a></td>
						<td><code>{JSON.stringify(item.payload)}</code></td>
					</tr>
				{/each}
			</tbody>
		</table>
		<p class="muted spaced">
			Act on these in CLI: <code>python -m apps.dedup decide</code>.
		</p>
	{/if}
{/if}

<style>
	.tabs {
		display: flex;
		gap: 0.4rem;
	}
	.muted {
		color: var(--muted);
	}
	.note {
		margin-top: 0.5rem;
	}
	.spaced {
		margin-top: 1rem;
	}
	.load-error {
		margin-top: 1rem;
		display: flex;
		align-items: center;
		gap: 0.75rem;
		color: var(--danger);
	}
	.load-error p {
		margin: 0;
	}
</style>
