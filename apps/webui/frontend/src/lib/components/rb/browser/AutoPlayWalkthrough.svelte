<script lang="ts">
	// Thin view over the pure autoplay-walkthrough.ts timeline - no timing
	// or compatibility logic lives here, only a requestAnimationFrame loop
	// copied from WaveRow.svelte's own pattern (mount starts, unmount stops)
	// and a lookup into the current frame via frameAt.
	import {
		buildWalkthroughTimeline,
		camelotHue,
		DEMO_ROWS,
		frameAt,
		rowTreatment,
		totalMs,
		type WalkthroughFrame,
		type WalkthroughMode
	} from '$lib/rb/autoplay-walkthrough';

	const { mode }: { mode: WalkthroughMode } = $props();

	const frames = $derived(buildWalkthroughTimeline(mode));

	let elapsedMs = $state(0);
	const playerState = $derived(frameAt(frames, elapsedMs));

	// Reject/strand/simulate share the frowning-face beat; the summary only
	// earns the blush once nothing is left stranded (SA5 4.4/4.6/AC8).
	function _emoji(frame: WalkthroughFrame): string | null {
		if (frame.phase === 'summary') return frame.strandedIds.length === 0 ? '\u{1F60A}' : null;
		if (frame.phase === 'strand' || frame.phase === 'simulate') {
			return frame.strandedIds.length > 0 ? '\u{1F643}' : null;
		}
		if (frame.phase === 'reject') return frame.focusIds.length > 0 ? '\u{1F643}' : null;
		return null;
	}
	const summaryEmoji = $derived(_emoji(playerState.frame));

	function _rowEmoji(rowId: string): string | null {
		const treatment = rowTreatment(rowId, playerState.frame);
		return treatment === 'flash' || treatment === 'stranded' ? '\u{1F643}' : null;
	}

	function _tileStyle(rowId: string): string {
		const row = DEMO_ROWS.find((r) => r.id === rowId);
		const hue = row === undefined ? null : camelotHue(row.key);
		return hue === null ? '' : `background: hsl(${hue}, 55%, 32%);`;
	}

	const ROW_HEIGHT_PX = 20;

	/** Skipped rows compact into a bottom tray; every other row keeps its
	 * fixture-order relative position. A fixed DOM order + CSS transform
	 * transition ("reshuffle") - no FLIP library, no reordered each block. */
	const rankOrder = $derived.by(() => {
		const ids = DEMO_ROWS.map((r) => r.id);
		return [...ids].sort((a, b) => {
			const aSkipped = playerState.frame.skippedIds.includes(a);
			const bSkipped = playerState.frame.skippedIds.includes(b);
			if (aSkipped !== bSkipped) return aSkipped ? 1 : -1;
			return ids.indexOf(a) - ids.indexOf(b);
		});
	});

	function _rankStyle(rowId: string): string {
		const rank = rankOrder.indexOf(rowId);
		return `transform: translateY(${rank * ROW_HEIGHT_PX}px);`;
	}

	$effect(() => {
		const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
		if (reduced) {
			elapsedMs = totalMs(frames);
			return;
		}
		elapsedMs = 0;
		let last = performance.now();
		let raf = requestAnimationFrame(function loop(now) {
			const deltaMs = now - last;
			last = now;
			const total = totalMs(frames);
			// Loop when elapsed exceeds totalMs - restart the storyboard.
			elapsedMs = total <= 0 ? 0 : (elapsedMs + deltaMs) % total;
			raf = requestAnimationFrame(loop);
		});
		return () => cancelAnimationFrame(raf);
	});
</script>

<div class="ap-walk">
	<div class="ap-walk-rows" style={`height:${DEMO_ROWS.length * ROW_HEIGHT_PX}px;`}>
		{#each DEMO_ROWS as row (row.id)}
			{@const treatment = rowTreatment(row.id, playerState.frame)}
			<div
				class="ap-walk-row"
				class:accepted={treatment === 'accepted'}
				class:skipped={treatment === 'skipped'}
				class:stranded={treatment === 'stranded'}
				class:flash={treatment === 'flash'}
				style={_rankStyle(row.id)}
			>
				<span class="ap-walk-art" style={_tileStyle(row.id)} aria-hidden="true"></span>
				<span class="ap-walk-meta">{row.key} - {row.bpm} BPM</span>
				{#if _rowEmoji(row.id) !== null}
					<span class="ap-walk-emoji">{_rowEmoji(row.id)}</span>
				{/if}
			</div>
		{/each}
	</div>
	<p class="ap-walk-caption">
		{playerState.frame.caption}
		{#if summaryEmoji !== null}<span class="ap-walk-emoji">{summaryEmoji}</span>{/if}
	</p>
</div>

<style>
	.ap-walk {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}
	.ap-walk-rows {
		position: relative;
	}
	.ap-walk-row {
		position: absolute;
		top: 0;
		left: 0;
		right: 0;
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 2px 4px;
		border: 1px solid transparent;
		border-radius: 2px;
		background: color-mix(in srgb, var(--rb-text) 6%, transparent);
		transition:
			transform 220ms ease-out,
			background 180ms ease-out,
			border-color 180ms ease-out;
	}
	.ap-walk-art {
		display: block;
		width: 12px;
		height: 12px;
		border-radius: 2px;
		flex: none;
		background: var(--rb-border);
	}
	.ap-walk-meta {
		font-size: 10px;
		color: var(--rb-text-dim);
		white-space: nowrap;
	}
	.ap-walk-emoji {
		font-size: 11px;
		line-height: 1;
	}
	.ap-walk-row.accepted {
		background: color-mix(in srgb, var(--rb-green) 22%, transparent);
		border-color: color-mix(in srgb, var(--rb-green) 55%, transparent);
	}
	.ap-walk-row.skipped {
		background: color-mix(in srgb, var(--rb-text-dim) 12%, transparent);
		opacity: 0.6;
	}
	.ap-walk-row.stranded {
		border-color: var(--rb-text-dim);
		border-style: dashed;
		opacity: 0.8;
	}
	.ap-walk-row.flash {
		background: color-mix(in srgb, var(--rb-red) 35%, transparent);
		border-color: var(--rb-red);
		animation: ap-walk-flash 420ms ease-in-out infinite alternate;
	}
	.ap-walk-caption {
		margin: 0;
		display: flex;
		align-items: center;
		gap: 4px;
		font-size: 10px;
		color: var(--rb-text-dim);
		min-height: 13px;
	}
	@keyframes ap-walk-flash {
		from {
			opacity: 0.7;
		}
		to {
			opacity: 1;
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.ap-walk-row {
			transition: none;
		}
		.ap-walk-row.flash {
			animation: none;
		}
	}
</style>
