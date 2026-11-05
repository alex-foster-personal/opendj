<script lang="ts">
	import type { Track } from '$lib/api';
	import ProvenanceTooltipView from './ProvenanceTooltipView.svelte';
	let { track, field }: { track: Track; field: string } = $props();
	const prov = $derived(track.provenance?.[field]);
	const title = $derived(
		prov ? `${prov.source} · conf ${prov.confidence ?? '?'} · ${prov.modified_at}` : null
	);
</script>

<ProvenanceTooltipView {title}><slot /></ProvenanceTooltipView>
