<script lang="ts">
	// SET-10 / SET-12: the REC button's source picker, a MACHINE | LOOPBACK |
	// MIDI toggle with one view each. MACHINE (the default) records Open DJ's
	// own master mix, which needs no driver, as every other DJ app does.
	// LOOPBACK lists loopback inputs (BlackHole) and links an installer. MIDI is
	// a hardware mixer or controller whose mix comes back through an audio
	// interface input, so it lists every other input. Inputs are listed BY NAME
	// from the daemon and sent by name, so a choice still means the same input
	// after something else is plugged in. "Tracklist only" is always reachable.
	import { onMount } from 'svelte';
	import type { RecorderDevices, RecorderStatus } from '../../../../routes/sets/sets-api';
	import { getRecorderStatus } from '../../../../routes/sets/sets-api';
	import {
		INSTALL_LOOPBACK_URL,
		RECORD_MODES,
		defaultChoiceForMode,
		devicesForMode,
		getRememberedInput,
		initialRecordSelection,
		listRecorderDevices,
		masterMixUnavailableReason,
		startPerformanceRecorder,
		type RecordInputChoice,
		type RecordMode
	} from '$lib/sets/record-input-choice';
	import { nativeShellKind, openExternal } from '$lib/shell/native-shell';
	import RecordInputPickerView, { MASTER_VALUE, NONE_VALUE } from './RecordInputPickerView.svelte';

	let {
		onstarted,
		oncancel,
		notify,
		variant = 'modal'
	}: {
		/** Called with the live status once recording has started. */
		onstarted: (status: RecorderStatus) => void;
		oncancel: () => void;
		/** The rail's toast sink, passed in so this lazy chunk adds no store importer. */
		notify: (message: string, level: 'info' | 'error') => void;
		/** modal: full-screen backdrop (browser rail). popover: compact top-bar panel. */
		variant?: 'modal' | 'popover';
	} = $props();

	const masterUnavailable = masterMixUnavailableReason();

	let busy = $state(false);
	let devices = $state<RecorderDevices | null>(null);
	let recordingsDir = $state<string | null>(null);
	let loading = $state(true);
	let listError = $state<string | null>(null);
	let rememberError = $state<string | null>(null);
	let mode = $state<RecordMode>('machine');
	let selected = $state<string | null>(null);
	let startButton = $state<HTMLButtonElement | null>(null);

	const listed = $derived(devicesForMode(devices, mode));

	function toValue(choice: RecordInputChoice | null): string | null {
		if (choice === null) return null;
		if (choice.kind === 'device') return choice.name;
		return choice.kind === 'none' ? NONE_VALUE : MASTER_VALUE;
	}

	function toChoice(value: string): RecordInputChoice {
		if (value === NONE_VALUE) return { kind: 'none' };
		return value === MASTER_VALUE ? { kind: 'master' } : { kind: 'device', name: value };
	}

	function reason(error: unknown): string {
		return error instanceof Error ? error.message : String(error);
	}

	function switchMode(next: RecordMode): void {
		if (next === mode) return;
		mode = next;
		selected = toValue(defaultChoiceForMode(next, devices, masterUnavailable === null));
	}

	async function installLoopback(): Promise<void> {
		try {
			if (nativeShellKind() !== null) await openExternal(INSTALL_LOOPBACK_URL);
			else window.open(INSTALL_LOOPBACK_URL, '_blank', 'noopener');
		} catch (error) {
			notify(`Could not open ${INSTALL_LOOPBACK_URL}: ${reason(error)}`, 'error');
		}
	}

	onMount(() => {
		void (async () => {
			// Each failure is shown: an unreadable remembered input must not look
			// like "nothing remembered yet" (SET-10), and MACHINE needs no listing.
			const [inputs, remembered, status] = await Promise.allSettled([
				listRecorderDevices(),
				getRememberedInput(),
				getRecorderStatus()
			]);
			if (inputs.status === 'fulfilled') devices = inputs.value;
			else listError = reason(inputs.reason);
			if (remembered.status === 'rejected') rememberError = reason(remembered.reason);
			if (status.status === 'fulfilled') recordingsDir = status.value.recordings_dir ?? null;
			loading = false;
			const last = remembered.status === 'fulfilled' ? remembered.value : null;
			const initial = initialRecordSelection(last, devices, masterUnavailable === null);
			mode = initial.mode;
			selected = toValue(initial.choice);
			queueMicrotask(() => startButton?.focus());
		})();
	});

	async function start(): Promise<void> {
		if (selected === null || busy) return;
		const choice = toChoice(selected);
		busy = true;
		try {
			const status = await startPerformanceRecorder(choice, mode);
			const from =
				choice.kind === 'device'
					? `from ${choice.name}`
					: choice.kind === 'master'
						? 'master mix'
						: 'tracklist only';
			notify(`Recording ${status.session_id} (${from})`, 'info');
			onstarted(status);
		} catch (error) {
			notify(`REC failed: ${String(error)}`, 'error');
		} finally {
			busy = false;
		}
	}
</script>

<RecordInputPickerView
	modes={RECORD_MODES}
	{mode}
	bind:selected
	bind:startButton
	{loading}
	{busy}
	devicesLoaded={devices !== null}
	{listed}
	{listError}
	{rememberError}
	{masterUnavailable}
	{recordingsDir}
	{variant}
	onswitchmode={switchMode}
	oninstall={() => void installLoopback()}
	onstart={() => void start()}
	{oncancel}
/>
