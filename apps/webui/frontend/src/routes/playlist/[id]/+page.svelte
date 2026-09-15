<script lang="ts">
	import { page } from '$app/stores';
	import { onMount } from 'svelte';
	import {
		getPlaylist,
		getPlaylistRbDjayDiff,
		planRbDjayPlaylistSync,
		type PlaylistDetail
	} from '$lib/api';
	import { hasComputedDiff } from '$lib/playlist-diff-view';
	import PlayItPanel from '$lib/components/PlayItPanel.svelte';

	let detail = $state<PlaylistDetail | null>(null);
	let playlistId = $state<string | null>(null);
	let syncSectionOpen = $state(false);
	let syncBusy = $state(false);
	let syncMessage = $state<string | null>(null);
	let rbDjayDiff = $state<{
		computed: boolean;
		reason?: string;
		diff: PlaylistDetail['diff'];
		summary?: Record<string, unknown>;
	} | null>(null);

	let hasDiff = $derived(
		detail !== null &&
			(hasComputedDiff(detail.diff) ||
				(rbDjayDiff?.computed === true && hasComputedDiff(rbDjayDiff.diff)))
	);

	let activeDiff = $derived(
		rbDjayDiff?.computed ? rbDjayDiff.diff : detail?.diff ?? { rb_only: [], djay_only: [], both: [], conflicts: [] }
	);

	onMount(async () => {
		const id = $page.params.id;
		if (id === undefined) throw new Error('playlist route param "id" missing');
		playlistId = id;
		detail = await getPlaylist(id);
	});

	async function computeDryRunDiff() {
		if (!playlistId || !detail) return;
		syncBusy = true;
		syncMessage = null;
		try {
			const plan = await planRbDjayPlaylistSync({});
			syncMessage = `Plan ready (${plan.summary?.membership_adds ?? 0} adds, ${plan.summary?.membership_removes ?? 0} removes).`;
			rbDjayDiff = await getPlaylistRbDjayDiff(playlistId);
		} catch (err) {
			syncMessage = err instanceof Error ? err.message : 'RB/djay dry-run failed';
		} finally {
			syncBusy = false;
		}
	}
</script>

{#if detail}
	<a href="/">&larr; back</a>
	<h2>{detail.name}</h2>
	<p style="color: var(--muted);">Review the current playlist and preview a PLAY IT reorder before applying it.</p>
	<p><a href={`/playlist/${detail.playlist_id}/writeback`}>Write back to rekordbox / djay &rarr;</a></p>
	{#if playlistId}
		<PlayItPanel playlistId={playlistId} />
	{/if}

	<section style="margin-top: 1.5rem;">
		<button type="button" onclick={() => (syncSectionOpen = !syncSectionOpen)}>
			{syncSectionOpen ? 'Hide' : 'Show'} RB/djay sync
		</button>
		{#if syncSectionOpen}
			<p style="color: var(--muted);">
				Compute a dry-run playlist diff via
				<code>POST /api/v1/rb-djay-sync/playlists/plan</code>.
			</p>
			<button type="button" disabled={syncBusy} onclick={computeDryRunDiff}>
				{syncBusy ? 'Computing...' : 'Compute RB/djay dry-run'}
			</button>
			{#if syncMessage}
				<p style="color: var(--muted);">{syncMessage}</p>
			{/if}
		{/if}
	</section>

	{#if hasDiff}
		<div class="diff-columns">
			<div>
				<h3>RB only</h3>
				<ul>{#each activeDiff.rb_only as sid}<li><a href={`/track/${sid}`}>{sid}</a></li>{/each}</ul>
			</div>
			<div>
				<h3>Both</h3>
				<ul>{#each activeDiff.both as sid}<li><a href={`/track/${sid}`}>{sid}</a></li>{/each}</ul>
			</div>
			<div>
				<h3>djay only</h3>
				<ul>{#each activeDiff.djay_only as sid}<li><a href={`/track/${sid}`}>{sid}</a></li>{/each}</ul>
			</div>
		</div>
		{#if activeDiff.conflicts.length > 0}
			<h3>Conflicts</h3>
			<ul>
				{#each activeDiff.conflicts as c}
					<li>
						<a href={`/track/${c.stable_id}`}>{c.stable_id}</a>
						{#if c.rb_position !== null && c.djay_position !== null}
							(RB pos {c.rb_position}, djay pos {c.djay_position})
						{:else}
							({c.kind ?? 'conflict'})
						{/if}
					</li>
				{/each}
			</ul>
		{/if}
	{:else}
		<p style="color: var(--muted);">Rekordbox/djay sync diff not computed for this playlist.</p>
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
