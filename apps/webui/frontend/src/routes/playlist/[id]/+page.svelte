<script lang="ts">
	import { page } from '$app/stores';
	import { onMount } from 'svelte';
	import { getPlaylist, type PlaylistDetail } from '$lib/api';
	import PlayItPanel from '$lib/components/PlayItPanel.svelte';

	let detail = $state<PlaylistDetail | null>(null);
	let playlistId = $state<string | null>(null);

	onMount(async () => {
		const id = $page.params.id;
		if (id === undefined) throw new Error('playlist route param "id" missing');
		playlistId = id;
		detail = await getPlaylist(id);
	});
</script>

{#if detail}
	<a href="/">&larr; back</a>
	<h2>{detail.name}</h2>
	<p style="color: var(--muted);">Reviewing only. Apply changes via CLI: <code>python -m apps.sync.playlist_apply</code>.</p>
	{#if playlistId}
		<PlayItPanel playlistId={playlistId} />
	{/if}
	<div class="diff-columns">
		<div>
			<h3>RB only</h3>
			<ul>{#each detail.diff.rb_only as sid}<li><a href={`/track/${sid}`}>{sid}</a></li>{/each}</ul>
		</div>
		<div>
			<h3>Both</h3>
			<ul>{#each detail.diff.both as sid}<li><a href={`/track/${sid}`}>{sid}</a></li>{/each}</ul>
		</div>
		<div>
			<h3>djay only</h3>
			<ul>{#each detail.diff.djay_only as sid}<li><a href={`/track/${sid}`}>{sid}</a></li>{/each}</ul>
		</div>
	</div>
	{#if detail.diff.conflicts.length > 0}
		<h3>Conflicts</h3>
		<ul>
			{#each detail.diff.conflicts as c}
				<li>
					<a href={`/track/${c.stable_id}`}>{c.stable_id}</a>
					(RB pos {c.rb_position}, djay pos {c.djay_position})
				</li>
			{/each}
		</ul>
	{/if}
{:else}
	<p>Loading...</p>
{/if}

<style>
	.diff-columns {
		display: grid;
		grid-template-columns: repeat(3, 1fr);
		gap: 1rem;
	}
</style>
