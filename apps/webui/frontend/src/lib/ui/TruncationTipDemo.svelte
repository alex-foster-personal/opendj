<script lang="ts">
	// Story harness for `truncationTip` (LIBUX-57): one single-line label in a
	// box of `width` px, clipped with an ellipsis the way a track name is.
	import { truncationTip } from './truncation-tip';

	let { text, width }: { text: string; width: number } = $props();
</script>

<span class="truncation-tip-demo" style={`width:${width}px`} data-custom-tip="" use:truncationTip>{text}</span>

<style>
	.truncation-tip-demo {
		display: block;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: 12px;
	}
</style>
