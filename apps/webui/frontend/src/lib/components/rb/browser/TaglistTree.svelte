<script lang="ts">
	import { onMount } from 'svelte';
	import { subscribeKind, subscribeResync } from '$lib/api/events-bus';
	import { listMyTags, type MyTagCatalog } from '$lib/rb/api-edit-suite';
	import { RbApiError } from '$lib/rb/api-rb-error';
	import type { PlaylistNode } from '$lib/rb/library-types';
	import TaglistTreeView from './TaglistTreeView.svelte';

	let {
		selectedId,
		onselect
	}: {
		selectedId: string | null;
		onselect: (node: PlaylistNode) => void;
	} = $props();

	let catalog: MyTagCatalog | null = $state(null);
	let error: string | null = $state(null);

	async function refresh(): Promise<void> {
		try {
			catalog = await listMyTags();
			error = null;
		} catch (exc) {
			error = exc instanceof RbApiError ? exc.code : String(exc);
		}
	}

	onMount(() => {
		const unkind = subscribeKind('mytags', () => {
			void refresh();
		});
		const unresync = subscribeResync(() => {
			void refresh();
		});
		void refresh();
		return () => {
			unkind();
			unresync();
		};
	});
</script>

<TaglistTreeView {catalog} {error} {selectedId} {onselect} />
