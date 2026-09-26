<script lang="ts">
	/** Compact comment-pin affordance for the app-shell topbar (FB-16). */
	import { commentPinSummaryTitle, feedbackState, armPinPlacement } from '$lib/rb/feedback-store.svelte';

	const COMMENT_UNAVAILABLE =
		'Comment pins - this daemon does not serve /api/v1/feedback, so dropping a pin is unavailable';

	const unavailable = $derived(feedbackState.availability === 'missing');
	const commentPinTitle = $derived.by(() => {
		if (unavailable) return COMMENT_UNAVAILABLE;
		if (feedbackState.availability === 'unknown')
			return 'Comment pins - probing the daemon for /api/v1/feedback';
		const summary = commentPinSummaryTitle(feedbackState.pins);
		return feedbackState.placementArmed
			? `${summary}. Click anywhere to drop a comment pin (Esc cancels)`
			: `${summary}. Click to drop a comment pin anywhere on the UI`;
	});
</script>

<button
	type="button"
	class="fb-shell-pin fb-place-skip"
	class:rb-inert={unavailable}
	class:armed={feedbackState.placementArmed}
	disabled={unavailable}
	title={commentPinTitle}
	aria-label="Drop a comment pin"
	aria-pressed={feedbackState.placementArmed}
	onclick={armPinPlacement}
>
	<svg width="11" height="10" viewBox="0 0 12 11" aria-hidden="true">
		<path
			d="M1.5 1.5 h9 v6 h-4.5 l-2.5 2.4 v-2.4 h-2 z"
			fill="none"
			stroke="currentColor"
			stroke-width="1.2"
			stroke-linejoin="round"
		/>
	</svg>
</button>

<style>
	.fb-shell-pin {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		height: 28px;
		width: 28px;
		padding: 0;
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 4px;
		color: var(--muted);
		cursor: pointer;
		line-height: 0;
	}
	.fb-shell-pin:hover:not(:disabled) {
		color: var(--accent);
		border-color: var(--accent);
	}
	.fb-shell-pin.armed {
		color: var(--accent);
		border-color: color-mix(in srgb, var(--accent) 55%, var(--border));
	}
</style>
