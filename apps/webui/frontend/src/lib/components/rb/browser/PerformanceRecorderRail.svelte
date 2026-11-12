<script lang="ts">
	import { onMount } from 'svelte';
	import { showWholeCollectionFromChrome } from '$lib/rb/playlist-history-chrome.svelte';
	import IconRail from './IconRail.svelte';
	import { openUsbExport } from '$lib/rb/usb-export.svelte';
	import { setRecordingEnabled } from '$lib/sets/recording-flag';
	import { usePerformanceRecorderChrome } from '$lib/sets/performance-recorder-chrome.svelte';
	import { pushToast } from '$lib/stores.svelte';

	let {
		source,
		onspotify,
		oncollection,
		recordable = true
	}: {
		source: 'collection' | 'spotify';
		onspotify: () => void;
		oncollection?: () => void;
		recordable?: boolean;
	} = $props();

	const chrome = usePerformanceRecorderChrome(() => recordable);

	onMount(() => chrome.mount());

	function showWholeCollection(event: MouseEvent): void {
		if (source !== 'collection') oncollection?.();
		showWholeCollectionFromChrome(event.timeStamp);
	}
</script>

<IconRail
	{source}
	{onspotify}
	oncollection={showWholeCollection}
	{recordable}
	recordingAvailable={setRecordingEnabled()}
	recording={chrome.rail.recording}
	recordingBusy={chrome.recorderBusy}
	recordingWaiting={chrome.rail.waiting}
	recordingTip={chrome.rail.tip}
	recordingStoppable={chrome.rail.stoppable}
	onrecord={() => void chrome.togglePerformanceRecording()}
	onusb={openUsbExport}
/>

{#if chrome.RecordInputPicker !== null}
	<chrome.RecordInputPicker
		variant={chrome.pickerVariant}
		onstarted={(status) => chrome.onPickerStarted(status)}
		oncancel={() => chrome.closePicker()}
		notify={pushToast}
	/>
{/if}
