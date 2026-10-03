<script lang="ts">
	import { onMount } from 'svelte';
	import { getRecorderStatus, type RecorderStatus } from '../../../../routes/sets/sets-api';
	import { recordRailState, stopPerformanceRecorder } from '$lib/sets/performance-recorder';
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
		recoverable: false,
		capture: 'none'
	});
	let rail = $derived(recordRailState(recorder));
	let recorderBusy = $state(false);
	// Lazy: the picker renders only after a REC click, so it stays out of the
	// /performance bundle budget (charged to other-lazy instead). Non-null
	// means the picker is open.
	let RecordInputPicker = $state<typeof import('./RecordInputPicker.svelte').default | null>(null);

	onMount(() => {
		void refreshRecorderStatus();
	});

	// SET-11: while the capture is starting or waiting on the macOS microphone
	// prompt, re-read it so REC lights the moment audio is really written; and
	// while it records, so an input that stops mid-set unlights REC.
	$effect(() => {
		const after = rail.poll;
		if (after === null) return;
		const timer = setTimeout(() => void refreshRecorderStatus(), after);
		return () => clearTimeout(timer);
	});

	let failureShown = false;
	$effect(() => {
		const failed = recorder.active && (recorder.capture === 'failed' || recorder.capture === 'stopped');
		if (failed && !failureShown) {
			pushToast('Set recording: the audio input stopped. Press REC to stop and keep what was recorded.', 'error');
		}
		failureShown = failed;
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
	recording={rail.recording}
	recordingBusy={recorderBusy}
	recordingWaiting={rail.waiting}
	recordingTip={rail.tip}
	onrecord={() => void togglePerformanceRecording()}
/>

{#if RecordInputPicker !== null}
	<RecordInputPicker
		onstarted={(status) => ((recorder = status), (RecordInputPicker = null))}
		oncancel={() => (RecordInputPicker = null)}
		notify={pushToast}
	/>
{/if}
