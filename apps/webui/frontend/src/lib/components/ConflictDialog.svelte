<script lang="ts">
	import type { Track } from '$lib/api';

	let {
		mine,
		current,
		onkeep_mine,
		ontake_theirs,
		onmerge_tags
	}: {
		mine: Partial<Track>;
		current: Track;
		onkeep_mine: () => void;
		ontake_theirs: () => void;
		onmerge_tags: () => void;
	} = $props();
</script>

<div class="conflict-dialog" role="dialog" aria-modal="true">
	<div class="panel">
		<h2>Conflict detected</h2>
		<p>
			Another device changed <strong>{current.title ?? current.stable_id}</strong> while you were
			editing. Choose how to resolve:
		</p>
		<table>
			<thead>
				<tr>
					<th></th>
					<th>Yours</th>
					<th>Server</th>
				</tr>
			</thead>
			<tbody>
				{#each Object.keys(mine) as key}
					<tr>
						<td>{key}</td>
						<td>{JSON.stringify(mine[key as keyof Track])}</td>
						<td>{JSON.stringify(current[key as keyof Track])}</td>
					</tr>
				{/each}
			</tbody>
		</table>
		<div class="actions">
			<button onclick={ontake_theirs}>Take theirs</button>
			<button onclick={onmerge_tags}>Merge tags</button>
			<button class="primary" onclick={onkeep_mine}>Keep mine</button>
		</div>
	</div>
</div>

<style>
	.panel table {
		width: 100%;
		border-collapse: collapse;
		margin-top: 1rem;
	}
	.panel td,
	.panel th {
		padding: 0.3rem;
		border-bottom: 1px solid var(--border);
		font-size: 0.9rem;
	}
</style>
