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
	 * made the loading indicator invisible for the entire "loading..." text
	 * state, which is the opposite of the point. `loading` is the real gate:
	 * while loading and progress is still null this shows an indeterminate
	 * track and received-row count, never a made-up percentage.
	 */
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

	const label = $derived.by((): string => {
		if (progress === null) return 'loading...';
		if (progress.total === null) return `loading... ${progress.loaded.toLocaleString()} rows received`;
		return `loading... ${progress.loaded.toLocaleString()} of ${progress.total.toLocaleString()} rows`;
	});
</script>

{#if loading || progress !== null}
	<div class="lli-root" role="status" aria-live="polite">
		<span class="lli-mark" aria-hidden="true"></span>
		<div
			class="lli-track"
			class:lli-indeterminate={pct === null}
			role="progressbar"
			aria-label={label}
			aria-valuemin={pct === null ? undefined : 0}
			aria-valuemax={pct === null ? undefined : 100}
			aria-valuenow={pct ?? undefined}
			title={pct === null ? 'Loading progress is not yet knowable' : `${pct}%`}
		>
			<div class="lli-bar" style={pct === null ? undefined : `width:${pct}%`}></div>
		</div>
		<span class="lli-label">{label}{#if rowsPerSecond > 0}<span class="lli-rate"> · {Math.round(rowsPerSecond)} rows/s</span>{/if}</span>
	</div>
{/if}

<style>
	.lli-root {
		display: grid;
		justify-items: center;
		gap: 5px;
		margin-top: 2px;
		padding: 6px 8px;
		color: var(--rb-text-dim);
		font-family: var(--rb-font);
		font-size: 10px;
		line-height: 1.2;
	}
	.lli-mark {
		width: 20px;
		height: 20px;
		background: currentColor;
		mask: url('/favicon.svg') center / contain no-repeat;
		animation: library-mark-reveal 180ms step-end both, library-mark-spin 420ms linear infinite;
	}
	.lli-label {
		white-space: nowrap;
	}
	.lli-rate {
		opacity: 0.8;
	}
	.lli-track {
		position: relative;
		width: min(220px, 70vw);
		height: 2px;
		background: color-mix(in srgb, #4fb2ff 18%, transparent);
		outline: 1px solid color-mix(in srgb, #4fb2ff 70%, transparent);
		overflow: hidden;
	}
	.lli-bar {
		height: 100%;
		background: #4fb2ff;
		transition: width 0.15s linear;
	}
	.lli-indeterminate .lli-bar {
		position: absolute;
		inset: 0 auto 0 -45%;
		width: 45%;
		animation: library-load-sweep 800ms ease-in-out infinite;
	}
	@keyframes library-mark-reveal {
		from { opacity: 0; }
		to { opacity: 1; }
	}
	@keyframes library-mark-spin {
		to { transform: rotate(360deg); }
	}
	@keyframes library-load-sweep {
		to { transform: translateX(325%); }
	}
	@media (prefers-reduced-motion: reduce) {
		.lli-mark { animation: library-mark-reveal 180ms step-end both; }
		.lli-indeterminate .lli-bar { animation: none; inset-inline-start: 28%; }
	}
</style>
