<script lang="ts">
	// Extracted from BrowserPanel.svelte (quality ratchet: frontend.max_fan_out) so the
	// three track-edit dialogs it hosts count as ONE import there instead of three. Pure
	// move: same three mutually-exclusive branches, same props, same behavior - "exactly
	// one is ever on screen" per the original comment this block carried in BrowserPanel.
	import BulkEditModal from './BulkEditModal.svelte';
	import FindReplaceModal from './FindReplaceModal.svelte';
	import MyTagEditorModal from './MyTagEditorModal.svelte';

	let {
		openModal,
		stableIds,
		etags,
		rows,
		onclose,
		onapplied
	}: {
		openModal: 'bulk-edit' | 'find-replace' | 'mytag' | null;
		stableIds: string[];
		etags: Record<string, string>;
		rows: { stable_id: string; rating: number | null; comments: string | null }[];
		onclose: () => void;
		onapplied: () => void;
	} = $props();
</script>

{#if openModal === 'find-replace'}
	<FindReplaceModal {stableIds} {etags} {onclose} {onapplied} />
{:else if openModal === 'bulk-edit'}
	<BulkEditModal {stableIds} {etags} {rows} {onclose} {onapplied} />
{:else if openModal === 'mytag'}
	<MyTagEditorModal {stableIds} {etags} {onclose} {onapplied} />
{/if}
