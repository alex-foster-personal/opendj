<script lang="ts">
	import { onMount } from 'svelte';
	import { getQueue, type QueueOut } from '$lib/api';

	let tab = $state<'dedup' | 'bad_beatgrid' | 'auto_cue'>('dedup');
	let queue = $state<QueueOut | null>(null);

	async function load(): Promise<void> {
		queue = await getQueue(tab);
	}

	function change(kind: typeof tab): void {
		tab = kind;
		load();
	}

	onMount(load);
</script>

<h2>Triage queues (read-only)</h2>
<div class="tabs">
	<button onclick={() => change('dedup')} class:primary={tab === 'dedup'}>Dedup</button>
	<button onclick={() => change('bad_beatgrid')} class:primary={tab === 'bad_beatgrid'}>Bad beatgrid</button>
	<button onclick={() => change('auto_cue')} class:primary={tab === 'auto_cue'}>Auto cue</button>
</div>

{#if queue}
	{#if queue.note}
		<p style="color: var(--muted); margin-top: 0.5rem;">{queue.note}</p>
	{/if}
	{#if queue.items.length === 0 && !queue.note}
		<p style="color: var(--muted); margin-top: 1rem;">Queue empty.</p>
	{/if}
	<table class="library" style="margin-top: 1rem;">
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
	<p style="color: var(--muted); margin-top: 1rem;">
		Act on these in CLI: <code>python -m apps.dedup decide</code>.
	</p>
{/if}

<style>
	.tabs {
		display: flex;
		gap: 0.4rem;
	}
</style>
