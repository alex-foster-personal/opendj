<script lang="ts">
	let { rating = $bindable(0), onchange }: { rating?: number | null; onchange?: (next: number) => void } = $props();
	function set(n: number): void {
		rating = n;
		onchange?.(n);
	}
</script>

<span class="star-row">
	{#each [1, 2, 3, 4, 5] as n}
		<span class="star" class:filled={rating != null && rating >= n} onclick={() => set(n)} aria-label={`Set rating to ${n}`}>★</span>
	{/each}
</span>

<style>
	.star-row {
		display: inline-flex;
		gap: 0.1rem;
	}
</style>
