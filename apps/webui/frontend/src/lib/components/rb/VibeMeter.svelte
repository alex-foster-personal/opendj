<script lang="ts">
	/**
	 * Center-topbar vibe meter. Mouse travel tops up linear charge (BE
	 * sensitivity); display is an S-curve. Thumbs are explicit marks.
	 * Fill turns green once display hits top 20%, purple at final 10%.
	 * History: localStorage via vibe.svelte.ts.
	 */
	import { onMount } from 'svelte';
	import { runPerformanceCommandFromUi } from '$lib/rb/performance-ipc.svelte';
	import { startVibeMeter, stopVibeMeter, vibeState } from '$lib/rb/vibe.svelte';

	onMount(() => {
		startVibeMeter();
		return () => stopVibeMeter();
	});

	const pct = $derived(Math.round(vibeState.display * 100));
	const title = $derived(
		`vibe ${pct}% - move mouse to top up (peak ${Math.round(vibeState.peak * 100)}%)` +
			(vibeState.config_ready
				? ` | sens ${vibeState.sensitivity} decay ${vibeState.decay_per_sec}`
				: '')
	);

	function recordFeedback(vote: 'bad' | 'good' | 'great', event: MouseEvent): void {
		void runPerformanceCommandFromUi({ type: 'feedback_mark', vote }, event.timeStamp);
	}
</script>

	<div class="vibe-wrap" data-topbar-priority="low">
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

	<div
		class="vibe"
		class:hot={vibeState.display >= 0.55}
		class:lit={vibeState.display >= 0.15}
		class:peak={vibeState.display >= 0.9}
		role="meter"
		aria-label="vibe"
		aria-valuemin={0}
		aria-valuemax={100}
		aria-valuenow={pct}
		title={title}
	>
		<span class="vibe-label">VIBE</span>
		<div class="vibe-track">
			<!-- gradient locked to track width: green at 80%, purple at 90% -->
			<div class="vibe-fill" style={`width: ${pct}%;`}></div>
		</div>
	</div>

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
	.vibe-wrap {
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

	.vibe {
		display: flex;
		align-items: center;
		gap: 5px;
		height: 18px;
		padding: 0 6px;
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		background: var(--rb-panel-raised);
		color: var(--rb-text-dim);
		opacity: 0.72;
		transition: opacity 120ms linear, border-color 120ms linear, color 120ms linear;
	}
	.vibe.lit {
		opacity: 0.95;
		color: var(--rb-text);
	}
	.vibe.hot {
		opacity: 1;
		color: var(--rb-orange);
		border-color: color-mix(in srgb, var(--rb-orange) 45%, var(--rb-border));
	}
	.vibe.peak {
		color: #c44dff;
		border-color: color-mix(in srgb, #c44dff 55%, var(--rb-border));
	}

	.vibe-label {
		font-size: 9px;
		letter-spacing: 0.1em;
		line-height: 1;
		font-weight: 600;
	}

	.vibe-track {
		position: relative;
		width: 64px;
		height: 5px;
		background: #060809;
		border: 1px solid var(--rb-border);
		overflow: hidden;
	}
	.vibe-fill {
		position: absolute;
		left: 0;
		top: 0;
		bottom: 0;
		/* gradient locked to full track width so zone colors sit at 80/90% */
		background-image: linear-gradient(
			to right,
			color-mix(in srgb, var(--rb-orange) 70%, var(--rb-yellow)) 0%,
			color-mix(in srgb, var(--rb-orange) 70%, var(--rb-yellow)) 80%,
			#00c853 80%,
			#00c853 90%,
			#c44dff 90%,
			#c44dff 100%
		);
		background-size: 64px 100%;
		background-repeat: no-repeat;
		box-shadow: none;
		transition: width 40ms linear;
	}
	.vibe.hot .vibe-fill {
		box-shadow: 0 0 6px color-mix(in srgb, var(--rb-orange) 55%, transparent);
	}
	.vibe.peak .vibe-fill {
		box-shadow: 0 0 8px color-mix(in srgb, #c44dff 65%, transparent);
	}
</style>
