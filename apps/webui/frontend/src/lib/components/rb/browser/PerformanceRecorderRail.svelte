<script lang="ts">
	import { onMount } from 'svelte';
	import { getRecorderStatus, type RecorderStatus } from '../../../../routes/sets/sets-api';
	import { captureFailureMessage, recordRailState, stopPerformanceRecorder } from '$lib/sets/performance-recorder';
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
	// SET-12: the recording this page's master-mix tap feeds, if any. The tap
	// module is lazy for the same reason as the picker.
	let tapSession: string | null = null;
	// True while REC is stopping: a status read that lands mid-stop must not
	// attach a second tap to the recording being stopped.
	let stopping = false;

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
		const failure = captureFailureMessage(recorder);
		if (failure !== null && !failureShown) pushToast(failure, 'error');
		failureShown = failure !== null;
	});

	// SET-12: a master recording (started here, by an agent, or before a
	// reload) is fed by this page. The start route already pushed the attach
	// to the leader page; this makes sure of it (the same one tap, never a
	// second) and gives the tap this page's toast. A tap that cannot start
	// stops the recording and says why.
	function syncMasterTap(status: RecorderStatus): void {
		const id = status.session_id;
		if (stopping || tapSession === id || !status.owned || status.capture_source !== 'master' || id === null) return;
		tapSession = id;
		import('$lib/sets/master-mix-capture')
			.then((m) => m.ensureMasterMixCapture(id, (why) => pushToast(`Set recording: ${why}`, 'error')))
			.catch(async (error: unknown) => {
				pushToast(`REC failed: the master mix could not be recorded: ${String(error)}`, 'error');
				tapSession = null;
				recorder = await stopPerformanceRecorder(status).catch(() => recorder);
			});
	}

	async function stopMasterTap(): Promise<void> {
		if (tapSession === null) return;
		tapSession = null;
		await (await import('$lib/sets/master-mix-capture')).stopMasterMixCapture();
	}

	function setRecorder(status: RecorderStatus): void {
		recorder = status;
		if (status.active) syncMasterTap(status);
		else void stopMasterTap();
	}

	async function refreshRecorderStatus(): Promise<void> {
		try {
			setRecorder(await getRecorderStatus());
		} catch (error) {
			pushToast(`REC status failed: ${String(error)}`, 'error');
		}
	}

	async function togglePerformanceRecording(): Promise<void> {
		recorderBusy = true;
		try {
			recorder = await getRecorderStatus();
			if (recorder.active) {
				stopping = true;
				try {
					await stopMasterTap();
					setRecorder(await stopPerformanceRecorder(recorder));
				} finally {
					stopping = false;
				}
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
		onstarted={(status) => (setRecorder(status), (RecordInputPicker = null))}
		oncancel={() => (RecordInputPicker = null)}
		notify={pushToast}
	/>
{/if}
