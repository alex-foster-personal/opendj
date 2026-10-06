<script lang="ts">
	// LIBUX-49: the library header's edit tools, in one place. The pencil menu
	// (B7) and the add-track search, which is parked: hidden for now, never
	// removed (B8). The search still mounts only for an editable playlist, and
	// agents add tracks through PUT /playlists/:id/tracks;
	// performance-library-toolbar.spec.ts drives both while it is hidden.
	import AddTrackSearch from './AddTrackSearch.svelte';
	import LibraryEditMenu from './LibraryEditMenu.svelte';

	let {
		hasSelection,
		editable,
		onpick,
		onadd
	}: {
		hasSelection: boolean;
		editable: boolean;
		onpick: (action: 'find-replace' | 'bulk-edit' | 'mytag') => void;
		onadd: (stableId: string) => void;
	} = $props();
</script>

<LibraryEditMenu {hasSelection} {onpick} />
{#if editable}
	<span class="add-track-parked" data-testid="add-track-parked"><AddTrackSearch {onadd} /></span>
{/if}

<style>
	.add-track-parked {
		display: none;
	}
</style>
