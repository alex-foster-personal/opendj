<script lang="ts">
	// SMART (issue #10024): the smartlist rule editor in a modal over the browser,
	// opened from the tree row (Edit rule... or double-click), as rekordbox's
	// Edit Intelligent Playlist does (mimicked). It hosts the same editor as the
	// /smartlists/[id] page. Escape or the close button leave without saving; a
	// save closes it, and the server's smartlists event refreshes the tree count
	// and the open pane.
	import type { SmartlistOut } from '$lib/smartlists/http';
	import EditSuiteModal from './EditSuiteModal.svelte';
	import SmartlistRuleEditor from '../smartlists/SmartlistRuleEditor.svelte';

	let {
		smartlist,
		onclose,
		onsaved
	}: {
		smartlist: { id: string; name: string };
		onclose: () => void;
		onsaved?: (saved: SmartlistOut) => void;
	} = $props();

	function saved(row: SmartlistOut): void {
		onsaved?.(row);
		onclose();
	}
</script>

<EditSuiteModal title={`Edit rule: ${smartlist.name}`} {onclose}>
	<div
		class="smartlist-rule-modal"
		data-testid="smartlist-rule-modal"
		data-smartlist-id={smartlist.id}
	>
		<SmartlistRuleEditor smartlistId={smartlist.id} onsaved={saved} />
	</div>
</EditSuiteModal>

<style>
	.smartlist-rule-modal {
		font-size: var(--rb-fs-label);
	}
</style>
