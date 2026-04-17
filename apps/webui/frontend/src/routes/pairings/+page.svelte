<script lang="ts">
	import { onMount } from 'svelte';
	import { createPairing, deletePairing, listPairings, type Pairing } from '$lib/api';
	import { pushToast } from '$lib/stores';

	let pairings = $state<Pairing[]>([]);
	let source = $state('');
	let fromId = $state('');
	let toId = $state('');
	let notes = $state('');

	async function load(): Promise<void> {
		pairings = await listPairings(source || undefined);
	}

	async function create(): Promise<void> {
		if (!fromId || !toId) {
			pushToast('Both track IDs required', 'error');
			return;
		}
		await createPairing({ from_stable_id: fromId, to_stable_id: toId, notes });
		fromId = toId = notes = '';
		await load();
		pushToast('Pairing created');
	}

	async function remove(p: Pairing): Promise<void> {
		// Client-side etag computation mirrors server: sha1(pairing_id + ":" + updated_at)
		const etag = await sha1Etag(p.pairing_id, p.updated_at);
		try {
			await deletePairing(p.pairing_id, etag);
			await load();
			pushToast('Deleted');
		} catch (exc) {
			pushToast(`Delete failed: ${exc}`, 'error');
		}
	}

	async function sha1Etag(id: string, mtime: string): Promise<string> {
		const bytes = new TextEncoder().encode(`${id}:${mtime}`);
		const hash = await crypto.subtle.digest('SHA-1', bytes);
		const hex = Array.from(new Uint8Array(hash))
			.map((b) => b.toString(16).padStart(2, '0'))
			.join('');
		return `"${hex}"`;
	}

	onMount(load);
</script>

<h2>Pairings</h2>
<form onsubmit={(e) => { e.preventDefault(); create(); }}>
	<input placeholder="From stable_id" bind:value={fromId} />
	<input placeholder="To stable_id" bind:value={toId} />
	<input placeholder="Notes (optional, max 1000)" bind:value={notes} maxlength="1000" />
	<button class="primary" type="submit">Add pairing</button>
</form>

<div style="margin-top: 1rem;">
	<label>Filter source: <select bind:value={source} onchange={load}>
		<option value="">all</option>
		<option value="manual">manual</option>
		<option value="learned">learned</option>
		<option value="ai">ai</option>
	</select></label>
</div>

<table class="library" style="margin-top: 1rem;">
	<thead>
		<tr><th>From</th><th>&rarr;</th><th>To</th><th>Source</th><th>Notes</th><th></th></tr>
	</thead>
	<tbody>
		{#each pairings as p}
			<tr>
				<td><a href={`/track/${p.from_stable_id}`}>{p.from_stable_id}</a></td>
				<td>{p.direction}</td>
				<td><a href={`/track/${p.to_stable_id}`}>{p.to_stable_id}</a></td>
				<td>{p.source}</td>
				<td>{p.notes ?? ''}</td>
				<td><button onclick={() => remove(p)}>×</button></td>
			</tr>
		{/each}
	</tbody>
</table>

{#if pairings.length === 0}
	<p style="color: var(--muted); margin-top: 1rem;">No pairings yet. Add your first one above.</p>
{/if}
