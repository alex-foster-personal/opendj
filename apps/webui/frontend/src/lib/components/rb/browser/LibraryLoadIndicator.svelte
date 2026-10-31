<script lang="ts">
	/**
	 * Honest, page-granular library load progress (pin ad59ac, follow-on to
	 * #937), drawn as an OVERLAY strip at the top edge of the track table
	 * (pins 1f9711b7, dd5fad7f, e452be6b; W16, LIBUX-50).
	 *
	 * Three placements each broke one half of the requirement. Mounted as a
	 * conditional sibling above the table it pushed the column headers down
	 * for the length of every load (pin 02717d4ea496). Moved into
	 * TrackTable's body overlay it stopped moving anything and instead sat on
	 * top of the first track rows. Then an always-reserved 18px strip fixed
	 * both, but left an empty band above the column headers whenever nothing
	 * was loading (W16: "loading bar space is still there when not loading").
	 * So the root is now in the layout at ZERO height and stays mounted (it
	 * carries role=status for screen readers), and the strip is an absolutely
	 * positioned child painted over the column-header row only while a load
	 * or search is in flight: nothing reserves space when idle, nothing
	 * shifts when a load starts or ends, and it never reaches the track rows.
	 *
	 * Its ink is the skin's accent token (--rb-accent, via --lli-ink), never
	 * a literal: blue on Default and Light, light gray on the Gothic /
	 * mono-dev skin (W16: "Loading bar is blue in gothic-skin not white").
	 * Not --rb-wave-vocal: that one follows the waveform palette, so a mono
	 * palette on Default would repaint the bar and Gothic's stays pale blue.
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
	import { loadRowsPerSecond, type LoadRateBaseline } from '$lib/rb/load-rate';

	let {
		loading,
		progress,
		searching = false
	}: {
		loading: boolean;
		progress: { loaded: number; total: number | null } | null;
		searching?: boolean;
	} = $props();

	// LIBUX-37: the rows/s figure is measured from a baseline, set whenever
	// progress goes null->non-null (a fresh load starting) or loaded goes
	// backwards (a new load superseding one already in flight for this pane).
	// It stays null, and nothing is shown, until the window is long enough.
	let baseline: LoadRateBaseline | null = null;
	let rowsPerSecond = $state<number | null>(null);

	$effect(() => {
		const p = progress;
		if (p === null) {
			baseline = null;
			rowsPerSecond = null;
			return;
		}
		const now = performance.now();
		if (baseline === null || p.loaded < baseline.loaded) baseline = { atMs: now, loaded: p.loaded };
		rowsPerSecond = loadRowsPerSecond(baseline, now, p.loaded);
	});

	const pct = $derived.by((): number | null => {
		if (progress === null || progress.total === null || progress.total <= 0) return null;
		return Math.min(100, Math.round((progress.loaded / progress.total) * 100));
	});

	const label = $derived.by((): string => {
		if (progress === null) return 'loading...';
		const verb = loading ? 'loading...' : 'loading more...';
		if (progress.total === null) return `${verb} ${progress.loaded.toLocaleString()} rows received`;
		return `${verb} ${progress.loaded.toLocaleString()} of ${progress.total.toLocaleString()} rows`;
	});
</script>

<div class="lli-root" role="status" aria-live="polite">
	{#if loading || progress !== null || searching}
		<div class="lli-strip">
			<span class="lli-mark" aria-hidden="true"></span>
			{#if searching}
				<span class="lli-search">searching whole collection...</span>
			{/if}
			{#if loading || progress !== null}
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
				<span class="lli-label">{label}{#if rowsPerSecond !== null}<span class="lli-rate" title="Rows received per second, measured since this load's first page over a window of at least one second"> · {Math.round(rowsPerSecond)} rows/s</span>{/if}</span>
			{/if}
		</div>
	{/if}
</div>

<style>
	/* Zero-height anchor: in flow so it sits exactly at the table's top edge,
	 * but it reserves no space whether or not anything is loading. */
	.lli-root {
		--lli-strip-h: 18px;
		--lli-ink: var(--rb-accent);
		position: relative;
		z-index: 6;
		flex: 0 0 0;
		height: 0;
		overflow: visible;
	}
	.lli-strip {
		position: absolute;
		inset: 0 0 auto 0;
		height: var(--lli-strip-h);
		box-sizing: border-box;
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 8px;
		padding: 0 8px;
		overflow: hidden;
		background: var(--rb-panel);
		border-bottom: 1px solid var(--rb-border);
		/* Clicks fall through to the header row beneath; only the readouts
		 * that carry a hover title take the pointer back (below). */
		pointer-events: none;
		color: var(--rb-text-dim);
		font-family: var(--rb-font);
		font-size: 10px;
		line-height: 1.2;
	}
	.lli-mark {
		flex: 0 0 auto;
		width: 12px;
		height: 12px;
		background: currentColor;
		mask: url('/favicon.svg') center / contain no-repeat;
		animation: library-mark-reveal 180ms step-end both, library-mark-spin 420ms linear infinite;
	}
	.lli-search {
		white-space: nowrap;
	}
	.lli-label {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.lli-rate {
		opacity: 0.8;
		pointer-events: auto;
	}
	.lli-track {
		position: relative;
		flex: 0 1 220px;
		min-width: 60px;
		height: 2px;
		pointer-events: auto;
		background: color-mix(in srgb, var(--lli-ink) 18%, transparent);
		outline: 1px solid color-mix(in srgb, var(--lli-ink) 70%, transparent);
		overflow: hidden;
	}
	.lli-bar {
		height: 100%;
		background: var(--lli-ink);
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
