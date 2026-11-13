<script lang="ts">
	// Issue #9740: the Rating column funnel, a minimum of 1 to 5 stars; unrated rows drop out
	// while it is set, like the tracks API rating_min (rekordbox Track Filter Rating, mimicked).
	import { triggerFloatingAction } from '$lib/ui/clamp-to-viewport';
	import { STAR_FILLED_PATH } from '$lib/ui/icon-glyphs';
	import {
		EMPTY_RATING_FILTER,
		ratingFilterActive,
		ratingFilterFor,
		type RatingColumnFilter
	} from './track-table-column-filters';

	let {
		filter,
		onpatch
	}: {
		filter: RatingColumnFilter;
		onpatch: (next: RatingColumnFilter) => void;
	} = $props();

	const STARS = [1, 2, 3, 4, 5] as const;

	let open = $state(false);
	let triggerEl = $state<HTMLButtonElement | null>(null);
	let popoverEl = $state<HTMLDivElement | null>(null);

	const active = $derived(ratingFilterActive(filter));
	const triggerTitle = $derived(
		active
			? `Rating filter: ${filter.minStars} stars and up (click to change)`
			: 'Filter rows by minimum rating'
	);

	function _clear(): void {
		onpatch(EMPTY_RATING_FILTER);
		_close();
	}

	function _close(): void {
		open = false;
		triggerEl?.focus();
	}

	function onWindowKeydown(event: KeyboardEvent): void {
		if (!open || event.key !== 'Escape') return;
		event.preventDefault();
		_close();
	}

	function onWindowPointerDown(event: PointerEvent): void {
		if (!open) return;
		const target = event.target;
		if (!(target instanceof Node)) return;
		if (triggerEl?.contains(target)) return;
		if (popoverEl?.contains(target)) return;
		_close();
	}
</script>

<svelte:window onkeydown={onWindowKeydown} onpointerdown={onWindowPointerDown} />

<span class="rating-funnel-pop">
	<button
		type="button"
		class="funnel-trigger"
		class:lit={active || open}
		bind:this={triggerEl}
		aria-expanded={open}
		aria-haspopup="dialog"
		aria-label="Rating filter"
		title={triggerTitle}
		data-testid="rating-funnel-trigger"
		onclick={() => (open = !open)}
	>
		<svg viewBox="0 0 16 16" width="9" height="9" aria-hidden="true">
			<path d="M2 3h12l-4.5 5v5l-3-1.5V8z" fill="currentColor" />
		</svg>
	</button>
	{#if open}
		<div
			class="funnel-float"
			role="dialog"
			aria-label="Rating column filter"
			bind:this={popoverEl}
			use:triggerFloatingAction={{ getTrigger: () => triggerEl, preferred: 'below', gap: 4 }}
		>
			<div class="stars">
				{#each STARS as stars (stars)}
					<button
						type="button"
						class="min"
						class:selected={filter.minStars === stars}
						aria-pressed={filter.minStars === stars}
						title={`${stars} stars and up`}
						aria-label={`${stars} stars and up`}
						data-testid={`rating-funnel-${stars}`}
						onclick={() => onpatch(ratingFilterFor(filter, stars))}
					>
						{#each STARS.slice(0, stars) as n (n)}
							<svg viewBox="0 0 24 24" width="1em" height="1em" aria-hidden="true">
								<path d={STAR_FILLED_PATH} fill="currentColor" />
							</svg>
						{/each}
					</button>
				{/each}
			</div>
			<div class="actions">
				<button type="button" class="clear" disabled={!active} onclick={_clear}>Clear</button>
			</div>
		</div>
	{/if}
</span>

<style>
	.rating-funnel-pop {
		position: relative;
		display: inline-flex;
		align-items: center;
		margin-left: 2px;
	}

	.funnel-trigger {
		padding: 0 1px;
		border: none;
		background: transparent;
		color: var(--rb-text-dim, #888);
		cursor: pointer;
		line-height: 0;
	}

	.funnel-trigger.lit {
		color: var(--rb-accent, #4fb2ff);
	}

	.funnel-float {
		z-index: 40;
		padding: 6px;
		background: var(--rb-panel-bg, #1a1a1a);
		border: 1px solid var(--rb-border, #444);
		border-radius: 4px;
		font-size: 10px;
	}

	.stars {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.min {
		font: inherit;
		padding: 2px 4px;
		border: 1px solid var(--rb-border, #555);
		border-radius: 2px;
		background: transparent;
		color: inherit;
		cursor: pointer;
		text-align: left;
		white-space: nowrap;
	}

	.min.selected {
		background: var(--rb-accent, #4fb2ff);
		color: var(--rb-on-accent, #fff);
	}

	.actions {
		margin-top: 6px;
		display: flex;
		justify-content: flex-end;
	}

	.clear {
		font: inherit;
		padding: 2px 6px;
		cursor: pointer;
	}

	.clear:disabled {
		opacity: 0.4;
		cursor: default;
	}
</style>
