<script lang="ts">
	/** "Save this order" (LIBUX-8678): shown while a playlist is sorted away from
	 * its stored order; pressing it makes the shown order the numbering. */
	let { busy = false, onsave }: { busy?: boolean; onsave: () => void } = $props();
</script>

<button
	type="button"
	class="rb-lit-button save-order"
	data-testid="save-playlist-order"
	disabled={busy}
	title="Make this order the playlist's own numbering (undoable)"
	onclick={onsave}
>
	{busy ? 'Saving order...' : 'Save this order'}
</button>

<style>
	.save-order {
		margin: 2px 8px;
		align-self: flex-start;
		font-size: 11px;
	}
</style>
