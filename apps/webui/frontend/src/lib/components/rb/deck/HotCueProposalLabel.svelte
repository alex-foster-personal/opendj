<script lang="ts">
	import { proposalCaption } from '$lib/rb/auto-cue-proposals';

	let { kind }: { kind: string } = $props();
</script>

<span class="proposal-label">{proposalCaption(kind)}</span>

<style>
	.proposal-label {
		flex: 1 1 auto;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-weight: 400;
		font-style: italic;
	}
	:global(.slot.proposal) {
		opacity: 0.8;
		border-left: 3px dashed var(--rb-accent);
	}
</style>
