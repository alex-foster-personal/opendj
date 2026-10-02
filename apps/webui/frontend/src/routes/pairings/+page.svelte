<script lang="ts">
	import { onMount } from 'svelte';
	import { createPairing, deletePairing, getTrack, listPairings, type Pairing } from '$lib/api';
	import { pushToast } from '$lib/stores.svelte';
	import { describeLoadError } from '$lib/route-load-state';

	let pairings = $state<Pairing[]>([]);
	let loading = $state(true);
	let loadError = $state<string | null>(null);
	let source = $state('');
	let fromId = $state('');
	let toId = $state('');
	let notes = $state('');
	// A filter change while a load is in flight must not let the older answer win.
	let loadSeq = 0;
	// "Artist - Title" per stable_id, so rows read as tracks rather than raw ids.
	// An id whose lookup fails stays shown as the id, with a tooltip saying why.
	let labels = $state<Record<string, string>>({});
	let missing = $state<Record<string, true>>({});

	async function resolveLabels(rows: Pairing[]): Promise<void> {
		const ids = new Set<string>();
		for (const p of rows) {
			ids.add(p.from_stable_id);
			ids.add(p.to_stable_id);
		}
		await Promise.all(
			[...ids]
				.filter((id) => !(id in labels) && !(id in missing))
				.map(async (id) => {
					try {
						const { track } = await getTrack(id);
						const name = track.title ?? id;
						labels[id] = track.artist ? `${track.artist} - ${name}` : name;
					} catch {
						missing[id] = true;
					}
				})
		);
	}

	async function load(): Promise<void> {
		const seq = ++loadSeq;
		loading = true;
		loadError = null;
		try {
			const next = await listPairings(source || undefined);
			if (seq !== loadSeq) return;
			pairings = next;
			void resolveLabels(next);
		} catch (exc) {
			if (seq !== loadSeq) return;
			pairings = [];
			loadError = describeLoadError(exc);
		} finally {
			if (seq === loadSeq) loading = false;
		}
	}

	async function create(): Promise<void> {
		if (!fromId || !toId) {
			pushToast('Both track IDs required', 'error');
			return;
		}
		try {
			await createPairing({ from_stable_id: fromId, to_stable_id: toId, notes });
		} catch (exc) {
			pushToast(`Add pairing failed: ${describeLoadError(exc)}`, 'error');
			return;
		}
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

	onMount(() => {
		void load();
	});
</script>

{#snippet trackCell(id: string)}
	<td><a href={`/track/${id}`} title={missing[id] ? `Track ${id} is not in the library` : id}>{labels[id] ?? id}</a></td>
{/snippet}

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

{#if loadError !== null}
	<div class="load-error" role="alert">
		<p>Could not load pairings: {loadError}</p>
		<button type="button" onclick={() => void load()}>Retry</button>
	</div>
{:else if loading && pairings.length === 0}
	<p class="muted spaced">Loading pairings...</p>
{:else if pairings.length === 0}
	<p class="muted spaced">
		{source === ''
			? 'No pairings yet. Add your first one above.'
			: `No ${source} pairings. Choose "all" to see every source.`}
	</p>
{:else}
	<table class="library spaced">
		<thead>
			<tr><th>From</th><th>&rarr;</th><th>To</th><th>Source</th><th>Notes</th><th></th></tr>
		</thead>
		<tbody>
			{#each pairings as p}
				<tr>
					{@render trackCell(p.from_stable_id)}
					<td>{p.direction}</td>
					{@render trackCell(p.to_stable_id)}
					<td>{p.source}</td>
					<td>{p.notes ?? ''}</td>
					<td><button onclick={() => remove(p)} title="Delete this pairing" aria-label="Delete this pairing">×</button></td>
				</tr>
			{/each}
		</tbody>
	</table>
{/if}

<style>
	.muted {
		color: var(--muted);
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
