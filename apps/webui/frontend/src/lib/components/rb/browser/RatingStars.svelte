<script lang="ts">
	import { STAR_FILLED_PATH, STAR_OUTLINE_PATH } from '$lib/ui/icon-glyphs';
	// Editable 5-star rating cell (SCREENSHOT-SPEC 5c: outline stars).
	// Clicking star n rates n; clicking the current rating clears to 0.
	// Hover previews the star count like a scrub (temporary highlight).
	// Persistence is the parent's job (PATCH /tracks/{sid} + If-Match).
	const STARS: number[] = [1, 2, 3, 4, 5];

	let {
		rating,
		onrate
	}: {
		rating: number | null;
		onrate: (next: number) => void;
	} = $props();

	let hoverN: number | null = $state(null);

	function _click(event: MouseEvent, n: number): void {
		// Keep row click/dblclick (select / deck load) out of rating edits.
		event.stopPropagation();
		onrate(rating === n ? 0 : n);
	}

	function _lit(n: number): boolean {
		if (hoverN !== null) return n <= hoverN;
		return rating !== null && rating >= n;
	}
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<span
	class="rb-stars"
	class:unrated={rating === null || rating === 0}
	role="radiogroup"
	aria-label="Rating"
	ondblclick={(e) => e.stopPropagation()}
	onpointerleave={() => (hoverN = null)}
>
	{#each STARS as n (n)}
		<button
			class="rb-star"
			class:filled={_lit(n)}
			class:preview={hoverN !== null && n <= hoverN}
			role="radio"
			aria-checked={rating !== null && rating >= n}
			aria-label={`Set rating ${n}`}
			title={`Set rating ${n}`}
			onpointerenter={() => (hoverN = n)}
			onclick={(e) => _click(e, n)}
			ondblclick={(e) => e.stopPropagation()}
		>
			<!-- 1em follows .rb-star font-size, which the rating cell shrinks via --rb-star-size. -->
			<svg viewBox="0 0 24 24" width="1em" height="1em" aria-hidden="true">
				<path
					d={_lit(n) ? STAR_FILLED_PATH : STAR_OUTLINE_PATH}
					fill={_lit(n) ? 'currentColor' : 'none'}
					stroke="currentColor"
					stroke-width="1.2"
				/>
			</svg>
		</button>
	{/each}
</span>

<style>
	/* Gap and glyph size come from the cell (see TrackTable .c-rating): it is
	 * the thing that knows how much room there is. Standalone fallbacks keep
	 * this component correct anywhere the variables are not set. */
	.rb-stars {
		display: inline-flex;
		flex-wrap: nowrap;
		max-width: 100%;
		gap: var(--rb-star-gap, 1px);
	}
	.rb-stars.unrated {
		gap: calc(3 * var(--rb-star-gap, 1px));
	}
	button {
		padding: 0;
		margin: 0;
		background: transparent;
		border: none;
		cursor: pointer;
	}
	/* colour comes from theme.css .perf-root .rb-star / .rb-star.filled */
</style>
