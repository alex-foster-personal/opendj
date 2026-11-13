<script lang="ts">
	import RecentlyDeletedFolderView from '$lib/components/rb/browser/RecentlyDeletedFolderView.svelte';
	import type { DeletedPlaylistOut } from '$lib/rb/playlist-deleted';

	// The component's own row type, so the harness cannot drift from the wire shape.
	type DeletedRow = DeletedPlaylistOut;

	let {
		initialOpen = true,
		initialRows = [] as DeletedRow[],
		initialError = null as string | null
	}: {
		initialOpen?: boolean;
		initialRows?: DeletedRow[];
		initialError?: string | null;
	} = $props();

	let open = $state(initialOpen);
	let rows = $state<DeletedRow[] | null>(initialRows);
	let error = $state<string | null>(initialError);

	export function setOpen(value: boolean): void {
		open = value;
	}

	export function setRows(value: DeletedRow[] | null): void {
		rows = value;
	}

	export function setError(value: string | null): void {
		error = value;
	}

	const noop = () => {};
	const noopRestore = (_id: string, _name: string) => {};
</script>

<RecentlyDeletedFolderView {open} {rows} {error} ontoggle={noop} onrestore={noopRestore} />
