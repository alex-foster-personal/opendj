<script lang="ts">
	import { onMount } from 'svelte';
	import { page } from '$app/stores';
	import {
		getSession,
		getTimeline,
		getTransitions,
		type SessionDetail,
		type TimelineEvent,
		type Transition
	} from '../../sets-api';

	type SharedSet = SessionDetail & { transitions: Transition[] };

	let sharedSet = $state<SharedSet | null>(null);
	let timeline = $state<TimelineEvent[]>([]);
	let loadError = $state<string | null>(null);

	const playedTracks = $derived(timeline.filter((event) => event.action === 'track_loaded'));

	onMount(loadSharedSet);

	async function loadSharedSet(): Promise<void> {
		const sessionId = $page.params.sessionId?.trim();
		if (!sessionId) {
			loadError = 'This share link does not identify a set.';
			return;
		}
		try {
			const [session, nextTimeline, transitions] = await Promise.all([
				getSession(sessionId),
				getTimeline(sessionId),
				getTransitions(sessionId)
			]);
			if (session.summary.share_state !== 'shared_cloud') {
				throw new Error('This set has not been published for metadata sharing.');
			}
			for (const event of nextTimeline) {
				if (event.action === 'track_loaded' && !event.track_stable_id && !eventTitle(event)) {
					throw new Error('A recorded track has neither a title nor a stable identifier.');
				}
			}
			sharedSet = { ...session, transitions };
			timeline = nextTimeline;
		} catch (error) {
			loadError = `${error}`;
		}
	}

	function eventTitle(event: TimelineEvent): string | null {
		const title = event.value.title;
		return typeof title === 'string' && title.trim() !== '' ? title : null;
	}

	function artist(event: TimelineEvent): string | null {
		const value = event.value.artist;
		return typeof value === 'string' && value.trim() !== '' ? value : null;
	}

	function trackTitle(event: TimelineEvent): string {
		return eventTitle(event) ?? event.track_stable_id ?? '';
	}

	function formatElapsed(seconds: number): string {
		const minutes = Math.floor(seconds / 60);
		return `${minutes}:${Math.floor(seconds % 60).toString().padStart(2, '0')}`;
	}

	function formatDuration(seconds: number | null): string {
		if (seconds === null) return 'Duration not finalized';
		return formatElapsed(seconds);
	}

	function transitionTrack(track: string | null, deck: string | null): string {
		if (track) return track;
		if (deck) return `Deck ${deck}`;
		return 'No recorded track';
	}
</script>

<svelte:head><title>Shared set - Open DJ</title></svelte:head>

<main class="share-page">
	<header>
		<p class="eyebrow">OPEN DJ SET HISTORY</p>
		<h1>Shared set</h1>
		<p class="lede">A real performance timeline, shared as metadata only.</p>
	</header>

	{#if loadError}
		<section class="error" aria-live="polite">
			<h2>Set unavailable</h2>
			<p>{loadError}</p>
		</section>
	{:else if sharedSet}
		<section class="hero" aria-label="Set overview">
			<div>
				<span>Started</span>
				<strong>{new Date(sharedSet.summary.started_at).toLocaleString()}</strong>
			</div>
			<div>
				<span>Duration</span>
				<strong>{formatDuration(sharedSet.summary.duration_s)}</strong>
			</div>
			<div>
				<span>Played</span>
				<strong>{playedTracks.length} tracks</strong>
			</div>
			<div>
				<span>Transitions</span>
				<strong>{sharedSet.transitions.length} detected</strong>
			</div>
		</section>

		<section class="tracklist" aria-label="Played track order">
			<div class="section-heading">
				<div>
					<p class="eyebrow">PERFORMANCE</p>
					<h2>Track order</h2>
				</div>
				<span>{playedTracks.length} real timeline events</span>
			</div>
			{#if playedTracks.length === 0}
				<p class="empty">This finalized set has no recorded track events.</p>
			{/if}
			<ol>
				{#each playedTracks as event, index (`${event.timestamp_s}-${index}`)}
					<li>
						<time>{formatElapsed(event.timestamp_s)}</time>
						<div>
							<strong>{trackTitle(event)}</strong>
							<small>{artist(event) ?? event.source}{event.deck ? ` · Deck ${event.deck}` : ''}</small>
						</div>
					</li>
				{/each}
			</ol>
		</section>

		<section class="transitions" aria-label="Detected transitions">
			<div class="section-heading">
				<div>
					<p class="eyebrow">FLOW</p>
					<h2>Transitions</h2>
				</div>
				<span>{sharedSet.transitions.length} from the recorded timeline</span>
			</div>
			{#if sharedSet.transitions.length === 0}
				<p class="empty">No transitions were recorded for this set.</p>
			{/if}
			{#each sharedSet.transitions as transition (transition.idx)}
				<div class="transition">
					<time>{formatElapsed(transition.t_change_s)}</time>
					<div>
						<strong>{transitionTrack(transition.from_track, transition.from_deck)} → {transitionTrack(transition.to_track, transition.to_deck)}</strong>
						<small>{transition.predicted_class} · {Math.round(transition.confidence * 100)}% confidence</small>
					</div>
				</div>
			{/each}
		</section>
	{:else}
		<p class="loading" aria-live="polite">Loading recorded set metadata...</p>
	{/if}

	<footer>
		Metadata-only share. Open DJ does not publish or stream recorded audio from this view.
	</footer>
</main>

<style>
	:global(body) { background: #081014; }
	.share-page { width: min(920px, calc(100% - 2rem)); margin: 0 auto; padding: clamp(1.5rem, 5vw, 4.5rem) 0 2rem; color: #e8f0f4; }
	header { margin-bottom: 2rem; }
	h1, h2, p { margin: 0; }
	h1 { font-size: clamp(2.4rem, 7vw, 5rem); letter-spacing: -0.05em; }
	.eyebrow { color: #6fc4de; font-size: 0.72rem; font-weight: 800; letter-spacing: 0.18em; }
	.lede, .empty, .loading { color: #a9bdc5; margin-top: 0.5rem; }
	.hero { display: grid; grid-template-columns: repeat(4, 1fr); gap: 1px; overflow: hidden; background: #2a4650; border: 1px solid #2a4650; border-radius: 12px; }
	.hero div { min-height: 105px; padding: 1rem; background: #102027; }
	.hero span, .section-heading span, small { display: block; color: #98adb5; font-size: 0.74rem; }
	.hero strong { display: block; margin-top: 0.45rem; font-size: 1rem; }
	.tracklist, .transitions, .error { margin-top: 1.5rem; background: #0d1a20; border: 1px solid #29434e; border-radius: 12px; padding: 1.2rem; }
	.section-heading { display: flex; align-items: end; justify-content: space-between; gap: 1rem; margin-bottom: 0.85rem; }
	.section-heading h2 { margin-top: 0.25rem; font-size: 1.4rem; }
	ol { list-style: none; margin: 0; padding: 0; counter-reset: tracks; }
	ol li, .transition { display: grid; grid-template-columns: 58px minmax(0, 1fr); gap: 0.85rem; padding: 0.8rem 0; border-top: 1px solid #213740; }
	ol li::before { content: counter(tracks, decimal-leading-zero); counter-increment: tracks; grid-column: 1; color: #5e8795; font: 0.7rem ui-monospace, monospace; }
	ol li time { grid-column: 1; grid-row: 1; margin-top: 1.2rem; color: #b4dbe6; font: 0.8rem ui-monospace, monospace; }
	ol li div { grid-column: 2; grid-row: 1 / span 2; }
	ol li strong, .transition strong { display: block; }
	ol li small, .transition small { margin-top: 0.25rem; }
	.transition time { color: #b4dbe6; font: 0.8rem ui-monospace, monospace; text-align: right; }
	.error { border-color: #914d52; background: #281518; }
	footer { color: #78919b; font-size: 0.75rem; margin-top: 2rem; text-align: center; }
	@media (max-width: 680px) {
		.hero { grid-template-columns: repeat(2, 1fr); }
		.section-heading { align-items: start; flex-direction: column; }
	}
</style>
