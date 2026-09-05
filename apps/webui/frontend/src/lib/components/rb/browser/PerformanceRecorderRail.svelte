<script lang="ts">
	import { onMount } from 'svelte';
	import { getRecorderStatus, type RecorderStatus } from '../../../../routes/sets/sets-api';
	import { startPerformanceRecorder, stopPerformanceRecorder } from '$lib/sets/performance-recorder';
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
			const input = window.prompt('ffmpeg audio input index for set recording');
			if (input === null) return;
			if (input.trim() === '') throw new Error('ffmpeg device index is required');
			recorder = await startPerformanceRecorder(Number(input));
			pushToast(`Recording ${recorder.session_id}`, 'info');
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
