<script lang="ts">
	// Props-only half of ProvenanceTooltip: the hover title is already
	// formatted by the container, so this only decides whether to wrap.
	import type { Snippet } from 'svelte';

	let { title, children }: { title: string | null; children?: Snippet } = $props();
</script>

{#if title !== null}
	<span {title}>
		{@render children?.()}
	</span>
{:else}
	{@render children?.()}
{/if}
