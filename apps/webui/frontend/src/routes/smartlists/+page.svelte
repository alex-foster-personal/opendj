<script lang="ts">
	import { onMount } from 'svelte';
	import { listSmartlists, type SmartlistOut } from '$lib/api';
	import { pushToast } from '$lib/stores.svelte';

	let smartlists = $state<SmartlistOut[]>([]);
	let loading = $state(true);

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
	Rule editing only; saving is not wired up yet. Author new smartlists via
	<code>python -m apps.smartlists.cli.create</code>.
</p>

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
