<script lang="ts">
	import { untrack } from 'svelte';
	import PerformanceRecorderRail from '$lib/components/rb/browser/PerformanceRecorderRail.svelte';

	let { initiallyRecordable = true }: { initiallyRecordable?: boolean } = $props();
	let recordable = $state(untrack(() => initiallyRecordable));

	export function setRecordable(value: boolean): void {
		recordable = value;
	}
</script>

<PerformanceRecorderRail source="collection" onspotify={() => {}} {recordable} />
