<script lang="ts">
	/**
	 * Center-topbar performance feedback thumbs (PREF-02). Each press records
	 * an explicit mark through the typed `feedback_mark` command: down = bad,
	 * up = good, Shift+up = great.
	 *
	 * The vibe meter that used to sit between them was removed (R10, the maintainer,
	 * Wed 7 Oct 2026: "remove the vibe meter. keep the two thumbs for now").
	 */
	import { runPerformanceCommandFromUi } from '$lib/rb/performance-ipc.svelte';

	function recordFeedback(vote: 'bad' | 'good' | 'great', event: MouseEvent): void {
		void runPerformanceCommandFromUi({ type: 'feedback_mark', vote }, event.timeStamp);
	}
</script>

<div class="feedback-thumbs" data-topbar-priority="low" data-feedback-thumbs-anchor>
	<button
		type="button"
		class="vibe-thumb down"
		aria-label="thumbs down"
		title="record performance: sounded bad"
		onclick={(event) => recordFeedback('bad', event)}
	>
		<svg width="11" height="11" viewBox="0 0 16 16" aria-hidden="true">
			<path d="M5 2.2h5.1l-.7 4.1h3c.8 0 1.3.8.9 1.5l-2 4.2c-.2.4-.6.7-1.1.7H5V2.2Zm-2 0h1v10.5H3a1 1 0 0 1-1-1v-8.5a1 1 0 0 1 1-1Z" fill="currentColor" />
		</svg>
	</button>

	<button
		type="button"
		class="vibe-thumb up"
		aria-label="thumbs up"
		title="record performance: sounded good. Shift+click or Shift+Enter: sounded great"
		onclick={(event) => recordFeedback(event.shiftKey ? 'great' : 'good', event)}
	>
		<svg width="11" height="11" viewBox="0 0 16 16" aria-hidden="true">
			<path d="M5 13.8h5.1l-.7-4.1h3c.8 0 1.3-.8.9-1.5l-2-4.2c-.2-.4-.6-.7-1.1-.7H5v10.5Zm-2 0h1V3.3H3a1 1 0 0 0-1 1v8.5a1 1 0 0 0 1 1Z" fill="currentColor" />
		</svg>
	</button>
</div>

<style>
	.feedback-thumbs {
		--vibe-thumb-up: #00c853;
		--vibe-thumb-down: color-mix(in srgb, var(--rb-red) 65%, var(--rb-text-dim));
		display: flex;
		align-items: center;
		gap: 3px;
	}

	.vibe-thumb {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 18px;
		height: 18px;
		padding: 0;
		background: var(--rb-panel-raised);
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		color: var(--rb-text-dim);
		cursor: pointer;
		line-height: 1;
	}
	.vibe-thumb.up {
		color: var(--vibe-thumb-up);
	}
	.vibe-thumb.down {
		color: var(--vibe-thumb-down);
	}
	.vibe-thumb.up:hover {
		color: #00e676;
		border-color: color-mix(in srgb, var(--vibe-thumb-up) 50%, var(--rb-border));
	}
	.vibe-thumb.down:hover {
		color: color-mix(in srgb, var(--rb-red) 78%, var(--rb-text-dim));
		border-color: color-mix(in srgb, var(--rb-red) 35%, var(--rb-border));
	}
	.vibe-thumb:focus-visible {
		outline: 1px solid var(--rb-accent);
		outline-offset: 1px;
	}
</style>
