<script lang="ts">
	/**
	 * PLACEHOLDER, and deliberately inert.
	 *
	 * `BrowserPanel.svelte` has imported this component since cfbfe55
	 * ("wip(spike)", Fri 24 Jul 2026 01:19) but the file was never committed,
	 * so `/performance` failed to build for anyone without that author's
	 * working tree. This restores the build WITHOUT inventing the feature.
	 *
	 * It renders nothing but an inert marker, per the house rule that a control
	 * with no real data source must not look live. The props below are exactly
	 * the ones BrowserPanel already passes, so replacing this file with the real
	 * implementation needs no change at the call site.
	 *
	 * TODO(owner of cfbfe55): replace with the real recommended-tracks section.
	 */
	import type { Track } from '$lib/api';

	type Props = {
		candidates?: unknown[];
		currentPlaylistId?: string | null;
		currentPlaylistMemberIds?: Set<string> | string[] | null;
		referenceBpm?: number | null;
		referenceKey?: string | null;
		onload?: (stableId: string) => void;
		onplay?: (stableId: string) => void;
		onhover?: (stableId: string) => void;
	};

	let { candidates = [] }: Props = $props();
</script>

{#if candidates.length > 0}
	<div
		class="rec-placeholder"
		title="Recommended section not implemented - the component was never committed (cfbfe55). See PARITY-TODO."
	>
		Recommended: not implemented ({candidates.length} candidate{candidates.length === 1
			? ''
			: 's'} available)
	</div>
{/if}

<style>
	.rec-placeholder {
		padding: 0.4rem 0.6rem;
		font-size: 0.78rem;
		color: var(--muted, #9aa4b2);
		border: 1px dashed var(--border, #1c222c);
		border-radius: 6px;
		margin: 0.4rem 0;
		cursor: help;
	}
</style>
