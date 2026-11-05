<script lang="ts">
	// SKINLAYOUT-8869b: the right panel of the three-panel library layout.
	// Every field comes from data the app already holds (the track row, the
	// last Gig session in localStorage, AutoPlay's charted order); nothing is
	// fetched from the web.
	import { getTrack, type Track } from '$lib/api';
	import { artworkUrl } from '$lib/rb/artwork-url';
	import { libraryWorkspace } from '$lib/library/library-workspace.svelte';
	import { libraryPreview } from '$lib/library/library-preview.svelte';
	import { autoPlayOrder } from '$lib/rb/autoplay-queue.svelte';
	import {
		PERFORMANCE_SESSION_STORAGE_KEY,
		parsePerformanceSession
	} from '$lib/rb/performance-session-snapshot';
	import {
		NOW_PLAYING_SOURCE_LABEL,
		aboutFacts,
		nextInQueueId,
		nowPlayingId
	} from '$lib/rb/skin-layout';

	function readSession(): ReturnType<typeof parsePerformanceSession> {
		try {
			return parsePerformanceSession(localStorage.getItem(PERFORMANCE_SESSION_STORAGE_KEY));
		} catch (exc) {
			console.error('[now-playing] stored Gig session unreadable:', exc);
			return null;
		}
	}
	const session = readSession();

	const pick = $derived(
		nowPlayingId({
			session,
			previewId: libraryPreview.stable_id,
			selectedId: libraryWorkspace.selected_ids.at(-1) ?? null // the row the preview bar shows
		})
	);
	const pickId = $derived(pick?.stable_id ?? null);
	const nextId = $derived(nextInQueueId(autoPlayOrder.chain, pickId));

	let track = $state<Track | null>(null);
	let next = $state<Track | null>(null);
	let loadError = $state<string | null>(null);
	let nextError = $state<string | null>(null);

	/** Each card owns its error, cleared on every id change, so a failed
	 * fetch never outlives the track it was for (Sentry thread, PR #8869). */
	function follow(
		sid: string | null,
		set: (t: Track | null) => void,
		fail: (why: string | null) => void
	): () => void {
		let live = true;
		set(null);
		fail(null);
		if (sid !== null) {
			getTrack(sid).then(
				(res) => {
					if (live) set(res.track);
				},
				(exc: unknown) => {
					if (live) fail(String(exc));
				}
			);
		}
		return () => {
			live = false;
		};
	}
	$effect(() =>
		follow(
			pickId,
			(t) => (track = t),
			(why) => (loadError = why)
		)
	);
	$effect(() =>
		follow(
			nextId,
			(t) => (next = t),
			(why) => (nextError = why)
		)
	);

	const facts = $derived(track === null ? [] : aboutFacts(track));
</script>

<aside
	class="now-playing"
	data-testid="now-playing-panel"
	aria-label="Now playing"
	data-surface="three-panel library layout"
>
	{#if pick === null}
		<p class="np-dim">Nothing playing. Select or preview a track, or load one in Gig.</p>
	{:else}
		<p class="np-dim" title="Where this track comes from">
			{NOW_PLAYING_SOURCE_LABEL[pick.source]}
		</p>
		<img class="np-art" src={artworkUrl(pick.stable_id, 'm')} alt="" />
		<h2 class="np-title" data-testid="now-playing-title">
			{track?.title ?? loadError ?? 'Loading...'}
		</h2>
		<p class="np-artist">{track?.artist ?? ''}</p>
		<section class="np-card" data-testid="now-playing-next">
			<h3>Next in queue</h3>
			{#if nextId === null}
				<p class="np-dim" title="AutoPlay charts an order once a playlist is open">
					AutoPlay has no next pick yet
				</p>
			{:else}
				<div class="np-next">
					<img src={artworkUrl(nextId, 's')} alt="" />
					<div>
						<p class="np-next-title">{next?.title ?? nextError ?? 'Loading...'}</p>
						<p class="np-dim">{next?.artist ?? ''}</p>
					</div>
				</div>
			{/if}
		</section>
		{#if facts.length > 0}
			<section class="np-card" data-testid="now-playing-about">
				<h3>About</h3>
				<dl>
					{#each facts as fact (fact.label)}
						<dt>{fact.label}</dt>
						<dd
							title={fact.label === 'Plays'
								? 'Times this track was played in Open DJ'
								: fact.label === 'BPM'
									? 'Beats per minute from analysis'
									: undefined}
						>
							{fact.value}
						</dd>
					{/each}
				</dl>
			</section>
		{/if}
	{/if}
</aside>

<style>
	.now-playing {
		grid-area: nowplaying;
		overflow-y: auto;
		padding: 16px;
		background: var(--rb-panel);
		border-radius: 8px;
		color: var(--rb-text);
		display: flex;
		flex-direction: column;
		gap: 8px;
		min-height: 0;
	}
	.np-dim {
		margin: 0;
		color: var(--rb-text-dim);
		font-size: 12px;
	}
	.np-art {
		width: 100%;
		aspect-ratio: 1;
		object-fit: cover;
		border-radius: 8px;
		background: var(--rb-bg);
	}
	.np-title {
		margin: 4px 0 0;
		font-size: 22px;
		font-weight: 700;
	}
	.np-artist {
		margin: 0;
		color: var(--rb-text-dim);
	}
	.np-card {
		background: var(--rb-bg);
		border-radius: 8px;
		padding: 12px;
	}
	.np-card h3 {
		margin: 0 0 8px;
		font-size: 14px;
		font-weight: 700;
	}
	.np-next {
		display: flex;
		gap: 10px;
		align-items: center;
	}
	.np-next img {
		width: 48px;
		height: 48px;
		border-radius: 4px;
		object-fit: cover;
	}
	.np-next-title {
		margin: 0;
		font-weight: 600;
	}
	dl {
		display: grid;
		grid-template-columns: auto 1fr;
		gap: 4px 12px;
		margin: 0;
		font-size: 13px;
	}
	dt {
		color: var(--rb-text-dim);
	}
	dd {
		margin: 0;
	}
</style>
