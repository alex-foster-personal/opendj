<!--
	PLAY-08: the persistent "AutoPlay has stopped and will not resume" banner.

	Issue #1640. Every terminal AutoPlay branch used to signal exactly once,
	through a toast that clears itself after five seconds. On Wed 9 Sep 2026 that
	toast fired at 18:23:39.614Z and the app then sat silent until 20:53Z with a
	loaded deck and nothing on screen saying why, which is what "it randomly
	stops playing" actually was.

	So this bar is deliberately not dismissible. It is displayed exactly while
	AutoPlay is stopped with no next track, and it goes away when the controller
	sees sound again (a different playing source) or AutoPlay is switched off.
	A dismiss button would restore the exact failure it exists to remove: an
	acknowledgement that leaves the room quiet and the screen blank.

	It is rendered OUTSIDE the /performance grid (position: fixed under the
	topbar) so it cannot reflow the decks or the waveform stack when it appears
	mid-set.
-->
<script lang="ts">
	import { autoPlayStall } from '$lib/rb/autoplay-stall.svelte';
	import { describeStallTrack, STALL_TRACK_LIMIT } from '$lib/rb/autoplay-stall';

	let showTracks = $state(false);
	/** Which stall the open/closed choice above was made about. */
	let expandedFor = $state<string | null>(null);

	const stall = $derived(autoPlayStall.current);
	const hidden = $derived(stall === null || stall.blocked_total <= stall.blocked.length ? 0 : stall.blocked_total - stall.blocked.length);
	/**
	 * A LATER stall starts collapsed (Codex r3973913201).
	 *
	 * The component stays mounted across stalls, so a bare `showTracks` left
	 * one operator's expansion armed for the next, unrelated stop - which then
	 * sprang a list over up to 30vh of the performance surface unasked. Keyed
	 * on the stall's identity rather than reset in an effect, so the open state
	 * cannot survive the thing it was about.
	 */
	const listOpen = $derived(
		showTracks && stall !== null && expandedFor === `${stall.reason}:${stall.source_stable_id}`
	);
</script>

{#if stall !== null}
	<div class="ap-stall" role="alert" data-testid="autoplay-stall-banner">
		<span class="ap-stall-head">{stall.headline}</span>
		<span class="ap-stall-resume">{stall.resume}</span>
		{#if stall.detail !== null}
			<span class="ap-stall-detail" title="The error the handoff reported">{stall.detail}</span>
		{/if}
		{#if stall.blocked.length > 0}
			<button
				type="button"
				class="ap-stall-toggle"
				aria-expanded={listOpen}
				title={`${stall.blocked_total} playlist track(s) AutoPlay could not use; the list names the first ${STALL_TRACK_LIMIT}`}
				onclick={() => {
					expandedFor = `${stall.reason}:${stall.source_stable_id}`;
					showTracks = !listOpen;
				}}
			>
				{listOpen ? 'Hide' : 'Show'} the {stall.blocked_total} track{stall.blocked_total === 1 ? '' : 's'}
			</button>
		{/if}
	</div>
	{#if listOpen && stall.blocked.length > 0}
		<ul class="ap-stall-list" data-testid="autoplay-stall-tracks">
			{#each stall.blocked as track (track.stable_id)}
				<li title={track.stable_id}>{describeStallTrack(track)}</li>
			{/each}
			{#if hidden > 0}
				<li class="ap-stall-more" title="Total minus the names shown above">
					and {hidden} more
				</li>
			{/if}
		</ul>
	{/if}
{/if}

<style>
	.ap-stall {
		position: fixed;
		top: var(--rb-topbar-h);
		left: 0;
		right: 0;
		z-index: 40;
		display: flex;
		align-items: baseline;
		justify-content: center;
		gap: 10px;
		flex-wrap: wrap;
		padding: 4px 10px;
		border-bottom: 1px solid color-mix(in srgb, var(--rb-red) 65%, var(--rb-border));
		background: color-mix(in srgb, var(--rb-red) 18%, var(--rb-panel));
		color: var(--rb-text);
		font-size: var(--rb-fs-label);
		line-height: 16px;
	}

	.ap-stall-head {
		font-weight: 600;
		color: var(--rb-red);
	}

	.ap-stall-resume,
	.ap-stall-detail {
		color: var(--rb-text-dim);
	}

	.ap-stall-toggle {
		border: 1px solid var(--rb-border);
		border-radius: 3px;
		padding: 0 6px;
		background: var(--rb-panel-raised);
		color: var(--rb-text);
		font-size: var(--rb-fs-label);
		line-height: 15px;
		cursor: pointer;
	}

	.ap-stall-list {
		position: fixed;
		top: calc(var(--rb-topbar-h) + 25px);
		left: 0;
		right: 0;
		z-index: 40;
		max-height: 30vh;
		overflow-y: auto;
		margin: 0;
		padding: 4px 10px 6px 26px;
		border-bottom: 1px solid color-mix(in srgb, var(--rb-red) 45%, var(--rb-border));
		background: var(--rb-panel);
		color: var(--rb-text-dim);
		font-size: var(--rb-fs-label);
		line-height: 15px;
	}

	.ap-stall-more {
		list-style: none;
		font-style: italic;
	}
</style>
