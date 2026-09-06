<script lang="ts">
	let { label = true, className = '' }: { label?: boolean; className?: string } = $props();
</script>

<span class={`odj-wordmark ${className}`} aria-label="Open DJ">
	<span class="odj-mark">oDj</span>
	{#if label}<span class="odj-label">open Dj</span>{/if}
</span>

<style>
	.odj-wordmark {
		display: inline-flex;
		align-items: baseline;
		gap: 0.32em;
		white-space: nowrap;
	}
	.odj-mark {
		color: #fff;
		font-family: Arial, Helvetica, sans-serif;
		font-size: 1.18em;
		font-style: italic;
		font-weight: 800;
		letter-spacing: -0.09em;
		line-height: 1;
	}
	.odj-label {
		color: currentColor;
		font-size: 0.68em;
		font-weight: 600;
		letter-spacing: 0.04em;
	}
</style>
