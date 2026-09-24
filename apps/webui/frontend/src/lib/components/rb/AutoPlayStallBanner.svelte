<!--
	PLAY-08 + issue #3882 pin 7c195d2d52ee: AutoPlay stall banner.

	Durable stall state stays in `autoPlayStall` (TopBar / IPC still expose it).
	The on-screen banner is a transient overlay: X dismiss, 5s timeout, 10s while
	hovered. It reappears only on a new stall revision.
-->
<script lang="ts">
	import { autoPlayStall } from '$lib/rb/autoplay-stall.svelte';
	import { describeStallTrack, STALL_TRACK_LIMIT } from '$lib/rb/autoplay-stall';
	import {
		autoplayStallBannerDismissMs,
		shouldShowAutoplayStallBanner
	} from '$lib/rb/autoplay-stall-banner-timer';

	let showTracks = $state(false);
	let expandedFor = $state<number | null>(null);
	let hiddenForRevision = $state<number | null>(null);
	let hovered = $state(false);
	let hideTimer: ReturnType<typeof setTimeout> | null = null;

	const stall = $derived(autoPlayStall.current);
	const hidden = $derived(stall === null || stall.blocked_total <= stall.blocked.length ? 0 : stall.blocked_total - stall.blocked.length);
	const listOpen = $derived(showTracks && stall !== null && expandedFor === stall.revision);
	const bannerVisible = $derived(
		stall !== null && shouldShowAutoplayStallBanner(stall.revision, hiddenForRevision)
	);

	function clearHideTimer(): void {
		if (hideTimer !== null) {
			clearTimeout(hideTimer);
			hideTimer = null;
		}
	}

	function hideBannerForCurrentRevision(): void {
		if (stall !== null) hiddenForRevision = stall.revision;
		clearHideTimer();
	}

	function scheduleHide(): void {
		clearHideTimer();
		hideTimer = setTimeout(() => hideBannerForCurrentRevision(), autoplayStallBannerDismissMs(hovered));
	}

	$effect(() => {
		if (!bannerVisible) {
			clearHideTimer();
			return;
		}
		scheduleHide();
	});
</script>

{#if stall !== null && bannerVisible}
	<div
		class="ap-stall-root"
		onmouseenter={() => {
			hovered = true;
			scheduleHide();
		}}
		onmouseleave={() => {
			hovered = false;
			scheduleHide();
		}}
	>
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
						const wasOpen = listOpen;
						expandedFor = stall.revision;
						showTracks = !wasOpen;
					}}
				>
					{listOpen ? 'Hide' : 'Show'} the {stall.blocked_total} track{stall.blocked_total === 1 ? '' : 's'}
				</button>
			{/if}
			<button
				type="button"
				class="ap-stall-dismiss"
				data-testid="autoplay-stall-dismiss"
				title="Dismiss this banner (AutoPlay remains stopped)"
				aria-label="Dismiss AutoPlay stopped banner"
				onclick={() => hideBannerForCurrentRevision()}>x</button
			>
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
	</div>
{/if}

<style>
	.ap-stall-root {
		position: fixed;
		top: var(--rb-topbar-h);
		left: 0;
		right: 0;
		z-index: 40;
	}

	.ap-stall {
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

	.ap-stall-dismiss {
		border: 1px solid var(--rb-border);
		border-radius: 3px;
		padding: 0 6px;
		background: var(--rb-panel-raised);
		color: var(--rb-red);
		font-size: var(--rb-fs-label);
		line-height: 15px;
		cursor: pointer;
	}

	.ap-stall-list {
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
