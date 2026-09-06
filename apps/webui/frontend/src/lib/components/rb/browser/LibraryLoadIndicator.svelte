<script lang="ts">
	/**
	 * Honest, page-granular library load progress (pin ad59ac, follow-on to
	 * #937). Mounted BETWEEN the pane header and TrackTable - never inside
	 * TrackTable, which stays a pure row renderer.
	 *
	 * "Honest" here means: the bar, the count, and the rows/s figure only
	 * ever move in the same whole-page jumps PaneStore.load_progress does
	 * (see pane-contract.svelte.ts updateLoadProgress / virtual-window.ts
	 * fetchAllPages onPage) - never interpolated or animated toward a guess.
	 * A stalled fetch shows as an unchanged count and speed rather than fake
	 * smooth motion, which is what makes a real hang visible instead of
	 * hidden behind reassuring animation - the whole point of the pin.
	 *
	 * The percentage is only ever shown once `total` is known (All Tracks
	 * health, or the selected playlist's track_count) - never invented.
	 *
	 * pin 0e8d6e -- `progress` alone is NOT the render gate. PaneStore.beginLoad
	 * resets load_progress to null for the WHOLE duration of a load, until the
	 * first page callback fires (All Tracks) or forever (an ordinary
	 * playlist load passes no onPage at all). Gating on `progress !== null`
	 * made the spinner invisible for the entire "loading..." text state, which
	 * is the opposite of the point. `loading` is the real gate: while loading
	 * and progress is still null this shows an INDETERMINATE state (spinner +
	 * "Loading" label, no bar) - an indeterminate spinner is still a spinner.
	 */
	import SpinnerIcon from './SpinnerIcon.svelte';

	let {
		loading,
		progress
	}: {
		loading: boolean;
		progress: { loaded: number; total: number | null } | null;
	} = $props();

	// Monotonic elapsed-time tracking for the rows/s figure. Resets whenever
	// progress goes null->non-null (a fresh load starting) or loaded goes
	// backwards (a new load superseding one already in flight for this pane).
	let startedAt: number | null = $state(null);
	let lastLoaded = $state(0);
	let rowsPerSecond = $state(0);

	$effect(() => {
		const p = progress;
		if (p === null) {
			startedAt = null;
			lastLoaded = 0;
			rowsPerSecond = 0;
			return;
		}
		const now = performance.now();
		if (startedAt === null || p.loaded < lastLoaded) {
			startedAt = now;
		}
		lastLoaded = p.loaded;
		const elapsedS = (now - (startedAt ?? now)) / 1000;
		rowsPerSecond = elapsedS > 0 ? p.loaded / elapsedS : 0;
	});

	const pct = $derived.by((): number | null => {
		if (progress === null || progress.total === null || progress.total <= 0) return null;
		return Math.min(100, Math.round((progress.loaded / progress.total) * 100));
	});
</script>

{#if loading || progress !== null}
	<div class="lli-root" role="status" aria-live="polite">
		<span class="lli-icon"><SpinnerIcon size={10} /></span>
		<span class="lli-label">
			{#if progress === null}
				Loading
			{:else}
				Loaded {progress.loaded}{progress.total !== null ? ` of ${progress.total}` : ''}
				{#if rowsPerSecond > 0}
					<span class="lli-rate">· {Math.round(rowsPerSecond)} rows/s</span>
				{/if}
			{/if}
		</span>
		{#if pct !== null}
			<div class="lli-track" title={`${pct}%`}>
				<div class="lli-bar" style={`width:${pct}%`}></div>
			</div>
		{/if}
	</div>
{/if}

<style>
	/* 2px lower than the pane header text baseline, per the pin - a small
	 * top margin does that without fighting the header's own layout. */
	.lli-root {
		display: flex;
		align-items: center;
		gap: 6px;
		margin-top: 2px;
		padding: 2px 8px;
		color: var(--rb-accent);
		font-family: var(--rb-font);
		font-size: 10px;
		line-height: 1.2;
	}
	.lli-icon {
		display: flex;
		flex: none;
		color: var(--rb-accent);
	}
	.lli-label {
		flex: none;
		color: var(--rb-accent);
		white-space: nowrap;
	}
	.lli-rate {
		opacity: 0.8;
	}
	.lli-track {
		flex: 1;
		max-width: 220px;
		height: 3px;
		border-radius: 2px;
		background: color-mix(in srgb, var(--rb-accent) 20%, transparent);
		overflow: hidden;
	}
	.lli-bar {
		height: 100%;
		background: var(--rb-accent);
		transition: width 0.15s linear;
	}
</style>
