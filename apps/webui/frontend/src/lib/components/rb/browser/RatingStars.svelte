<script lang="ts">
	// Editable 5-star rating cell (SCREENSHOT-SPEC 5c: outline stars).
	// Clicking star n rates n; clicking the current rating clears to 0.
	// Persistence is the parent's job (PATCH /tracks/{sid} + If-Match).
	const STARS: number[] = [1, 2, 3, 4, 5];

	let {
		rating,
		onrate
	}: {
		rating: number | null;
		onrate: (next: number) => void;
	} = $props();

	function _click(event: MouseEvent, n: number): void {
		// Keep row click/dblclick (select / deck load) out of rating edits.
		event.stopPropagation();
		onrate(rating === n ? 0 : n);
	}
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<span class="rb-stars" role="radiogroup" aria-label="Rating" ondblclick={(e) => e.stopPropagation()}>
	{#each STARS as n (n)}
		<button
			class="rb-star"
			class:filled={rating !== null && rating >= n}
			role="radio"
			aria-checked={rating !== null && rating >= n}
			aria-label={`Set rating ${n}`}
			title={`Set rating ${n}`}
			onclick={(e) => _click(e, n)}
			ondblclick={(e) => e.stopPropagation()}
		>
			{rating !== null && rating >= n ? '★' : '☆'}
		</button>
	{/each}
</span>

<style>
	.rb-stars {
		display: inline-flex;
		gap: 1px;
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
