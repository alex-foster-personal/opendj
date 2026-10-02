<script lang="ts">
	import { onMount } from 'svelte';
	import { getRecorderStatus, type RecorderStatus } from '../../../../routes/sets/sets-api';
	import {
		rememberInput,
		startPerformanceRecorder,
		stopPerformanceRecorder,
		type RecordInputChoice
	} from '$lib/sets/performance-recorder';
	import { pushToast } from '$lib/stores.svelte';
	import IconRail from './IconRail.svelte';
	import RecordInputPicker from './RecordInputPicker.svelte';

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
	let pickerOpen = $state(false);

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
			pickerOpen = true;
		} catch (error) {
			pushToast(`REC failed: ${String(error)}`, 'error');
		} finally {
			recorderBusy = false;
		}
	}

	async function startWithInput(choice: RecordInputChoice): Promise<void> {
		recorderBusy = true;
		try {
			recorder = await startPerformanceRecorder(choice);
			rememberInput(choice);
			pickerOpen = false;
			const from = choice.kind === 'device' ? `from ${choice.name}` : 'tracklist only';
			pushToast(`Recording ${recorder.session_id} (${from})`, 'info');
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

{#if pickerOpen}
	<RecordInputPicker
		busy={recorderBusy}
		onstart={(choice) => void startWithInput(choice)}
		oncancel={() => (pickerOpen = false)}
	/>
{/if}
