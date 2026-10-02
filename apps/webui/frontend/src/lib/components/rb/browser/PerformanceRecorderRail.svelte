<script lang="ts">
	import { onMount } from 'svelte';
	import { getRecorderStatus, type RecorderStatus } from '../../../../routes/sets/sets-api';
	import { stopPerformanceRecorder } from '$lib/sets/performance-recorder';
	import { pushToast } from '$lib/stores.svelte';
	import IconRail from './IconRail.svelte';

	let {
		source,
		onspotify
	}: {
		source: 'collection' | 'spotify';
		onspotify: () => void;
	} = $props();

	let recorder = $state<RecorderStatus>({
		active: false,
		session_id: null,
		pid: null,
		owned: false,
		recoverable: false
	});
	let recorderBusy = $state(false);
	// Lazy: the picker renders only after a REC click, so it stays out of the
	// /performance bundle budget (charged to other-lazy instead). Non-null
	// means the picker is open.
	let RecordInputPicker = $state<typeof import('./RecordInputPicker.svelte').default | null>(null);

	onMount(() => {
		void refreshRecorderStatus();
	});

	async function refreshRecorderStatus(): Promise<void> {
		try {
			recorder = await getRecorderStatus();
		} catch (error) {
			pushToast(`REC status failed: ${String(error)}`, 'error');
		}
	}

	async function togglePerformanceRecording(): Promise<void> {
		recorderBusy = true;
		try {
			recorder = await getRecorderStatus();
			if (recorder.active) {
				recorder = await stopPerformanceRecorder(recorder);
				pushToast('Recording stopped and session finalized.', 'info');
				return;
			}
			// SET-10: pick the input by name in-app. A browser prompt dialog never shows in
			// the desktop app's WKWebView, so the old index prompt did nothing.
			RecordInputPicker = (await import('./RecordInputPicker.svelte')).default;
		} catch (error) {
			pushToast(`REC failed: ${String(error)}`, 'error');
		} finally {
			recorderBusy = false;
		}
	}
</script>

<IconRail
	{source}
	{onspotify}
	recording={recorder.active}
	recordingBusy={recorderBusy}
	onrecord={() => void togglePerformanceRecording()}
/>

{#if RecordInputPicker !== null}
	<RecordInputPicker
		onstarted={(status) => ((recorder = status), (RecordInputPicker = null))}
		oncancel={() => (RecordInputPicker = null)}
	/>
{/if}
