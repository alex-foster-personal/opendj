<script lang="ts">
	import { sortArrowPath } from '$lib/ui/icon-glyphs';

	let { asc }: { asc: boolean } = $props();
</script>

<svg class="sort-arrow" viewBox="0 0 12 12" width="8" height="8" aria-hidden="true">
	<path d={sortArrowPath(asc)} fill="currentColor" />
</svg>

<style>
	.sort-arrow {
		display: inline-block;
		vertical-align: middle;
		margin-left: 2px;
	}
</style>
