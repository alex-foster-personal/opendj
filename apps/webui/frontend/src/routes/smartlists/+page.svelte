<script lang="ts">
	import { goto } from '$app/navigation';
	import { onMount } from 'svelte';
	import { createSmartlist, listSmartlists, type SmartlistOut } from '$lib/api';
	import { pushToast } from '$lib/stores.svelte';

	const STARTER_RULE = { field: 'rating', op: '>=', value: 0 } as const;

	let smartlists = $state<SmartlistOut[]>([]);
	let loading = $state(true);
	let newName = $state('');
	let creating = $state(false);
	let createError = $state<string | null>(null);

	async function create(): Promise<void> {
		const name = newName.trim();
		if (name.length === 0) {
			createError = 'Name a smartlist before creating it.';
			return;
		}
		creating = true;
		createError = null;
		try {
			const created = await createSmartlist({
				name,
				rule: { ...STARTER_RULE }
			});
			pushToast(`Created ${created.smartlist.name}.`);
			await goto(`/smartlists/${created.smartlist.id}`);
		} catch (exc) {
			createError = `${exc}`;
			pushToast(`Failed to create smartlist: ${exc}`, 'error');
		} finally {
			creating = false;
		}
	}

	onMount(async () => {
		try {
			smartlists = await listSmartlists();
		} catch (exc) {
			pushToast(`Failed to load smartlists: ${exc}`, 'error');
		} finally {
			loading = false;
		}
	});
</script>

<h2>Smartlists</h2>
<p style="color: var(--muted);">
	Name a new smartlist to open the rule editor. Saving is wired through the editor.
</p>

<form
	class="create-row"
	onsubmit={(event) => {
		event.preventDefault();
		void create();
	}}
>
	<label>
		Name
		<input bind:value={newName} placeholder="New smartlist" disabled={creating} />
	</label>
	<button class="primary" type="submit" disabled={creating || newName.trim().length === 0}>
		{creating ? 'Creating...' : 'Create smartlist'}
	</button>
	{#if createError}
		<p class="create-error" role="alert">{createError}</p>
	{/if}
</form>

{#if loading}
	<p>Loading...</p>
{:else if smartlists.length === 0}
	<p style="color: var(--muted);">No smartlists yet.</p>
{:else}
	<table class="library">
		<thead>
			<tr>
				<th>Name</th>
				<th>Order by</th>
				<th>Referenced fields</th>
				<th>Last evaluated</th>
			</tr>
		</thead>
		<tbody>
			{#each smartlists as s (s.id)}
				<tr onclick={() => (window.location.href = `/smartlists/${s.id}`)}>
					<td><a href={`/smartlists/${s.id}`}>{s.name}</a></td>
					<td>{s.order_by}</td>
					<td>
						{#each s.referenced_fields as field (field)}
							<span class="chip">{field}</span>
						{/each}
					</td>
					<td>{s.last_evaluated_at ?? 'never'}</td>
				</tr>
			{/each}
		</tbody>
	</table>
{/if}

<style>
	.create-row {
		display: flex;
		flex-wrap: wrap;
		gap: 0.5rem;
		align-items: end;
		margin: 0.5rem 0 1rem 0;
	}
	.create-row label {
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
	}
	.create-error {
		color: var(--danger);
		flex-basis: 100%;
		margin: 0;
	}
</style>
