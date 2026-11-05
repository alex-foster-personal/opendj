<script lang="ts">
	/** While a playlist with sections is sorted, says what happened to them and
	 * offers the way out (R-SEC-2, D7). */
	let {
		within,
		onwithin,
		onrestore
	}: {
		within: boolean;
		onwithin: (next: boolean) => void;
		onrestore: () => void;
	} = $props();
</script>

<div class="section-sort-bar" role="status" data-testid="section-sort-bar">
	<span>{within ? 'Sorted within sections.' : 'Sections are hidden while sorted.'}</span>
	<button type="button" class="rb-lit-button" aria-pressed={within} onclick={() => onwithin(!within)}>
		{within ? 'Sort across sections' : 'Sort within sections'}
	</button>
	<button type="button" class="rb-lit-button" onclick={onrestore}>Back to playlist order</button>
</div>

<style>
	.section-sort-bar {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 2px 8px;
		color: var(--rb-text-dim);
		font-size: 11px;
	}
</style>
