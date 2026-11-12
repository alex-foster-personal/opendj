<script lang="ts">
	import { untrack } from 'svelte';
	import RecorderRetryRail from './recorder-status-retry-rail.svelte';

	let { initiallyRecordable = true }: { initiallyRecordable?: boolean } = $props();
	let recordable = $state(untrack(() => initiallyRecordable));

	export function setRecordable(value: boolean): void {
		recordable = value;
	}
</script>

<RecorderRetryRail {recordable} />
