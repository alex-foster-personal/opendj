<script lang="ts">
	import type { Track } from '$lib/api';
	let { track, field }: { track: Track; field: string } = $props();
	const prov = $derived(track.provenance?.[field]);
</script>

{#if prov}
	<span title={`${prov.source} · conf ${prov.confidence ?? '?'} · ${prov.modified_at}`}>
		<slot />
	</span>
{:else}
	<slot />
{/if}
