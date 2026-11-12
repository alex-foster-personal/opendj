/** Shared /performance REC chrome: status polling, master tap, picker open/stop. */

import { getRecorderStatus, type RecorderStatus } from '../../routes/sets/sets-api';
import {
	captureFailureMessage,
	recordRailState,
	stopPerformanceRecorder,
	updateRetainedCaptureFailure,
	type RetainedCaptureFailure
} from './performance-recorder';
import { pushToast } from '$lib/stores.svelte';
import {
	REC_STATUS_UNREACHABLE_GROUP,
	REC_STATUS_UNREACHABLE_LIMIT,
	recorderStatusRetryMs,
	recorderStatusUnreachable,
	recorderStatusUnreachableMessage
} from './recorder-status-retry';
import {
	loadRecordInputPicker,
	type RecordInputPickerComponent
} from './record-input-picker-loader';

const idleStatus: RecorderStatus = {
	active: false,
	session_id: null,
	pid: null,
	owned: false,
	recoverable: false,
	capture: 'none',
	capture_error: null
};

let sharedChrome: ReturnType<typeof createPerformanceRecorderChrome> | null = null;
let recordableGate: (() => boolean) | null = null;

/** One chrome per page: the top-bar REC and browser rail share status and master tap. */
export function usePerformanceRecorderChrome(recordable: () => boolean) {
	recordableGate = recordable;
	if (sharedChrome === null) {
		sharedChrome = createPerformanceRecorderChrome(() => recordableGate?.() ?? false);
	}
	return sharedChrome;
}

export function createPerformanceRecorderChrome(recordable: () => boolean) {
	let recorder = $state<RecorderStatus>({ ...idleStatus });
	let retainedFailure = $state<RetainedCaptureFailure | null>(null);
	const rail = $derived(recordRailState(recorder, retainedFailure));
	let recorderBusy = $state(false);
	let RecordInputPicker = $state<RecordInputPickerComponent | null>(null);
	let pickerVariant = $state<'modal' | 'popover'>('modal');
	let tapSession: string | null = null;
	let stopping = false;
	let statusRetry = $state<{ after: number } | null>(null);
	let unreachableReads = 0;
	let failureShown = false;

	$effect(() => {
		if (statusRetry !== null) return;
		const after = rail.poll;
		if (after === null || !recordable()) return;
		const timer = setTimeout(() => void refreshRecorderStatus(), after);
		return () => clearTimeout(timer);
	});

	$effect(() => {
		const retry = statusRetry;
		if (retry === null || !recordable()) return;
		const timer = setTimeout(() => refreshRecorderStatus(), retry.after);
		return () => clearTimeout(timer);
	});

	$effect(() => {
		const failure = captureFailureMessage(recorder);
		if (failure !== null && !failureShown) pushToast(failure, 'error');
		failureShown = failure !== null;
	});

	function syncMasterTap(status: RecorderStatus): void {
		const id = status.session_id;
		if (
			stopping ||
			tapSession === id ||
			!status.owned ||
			status.capture_source !== 'master' ||
			id === null
		)
			return;
		tapSession = id;
		import('$lib/sets/master-mix-capture')
			.then((m) =>
				m
					.ensureMasterMixCapture(id, (why) => pushToast(`Set recording: ${why}`, 'error'))
					.catch((error: unknown) => {
						if (error instanceof m.MasterMixCancelled) {
							console.info(`master mix: ${error.message}`);
							return;
						}
						throw error;
					})
			)
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
		retainedFailure = updateRetainedCaptureFailure(retainedFailure, status);
		recorder = status;
		if (status.active) syncMasterTap(status);
		else void stopMasterTap();
	}

	async function refreshRecorderStatus(): Promise<void> {
		if (!recordable()) return;
		try {
			setRecorder(await getRecorderStatus());
			unreachableReads = 0;
			statusRetry = null;
		} catch (error) {
			if (!recorderStatusUnreachable(error)) {
				unreachableReads = 0;
				statusRetry = null;
				pushToast(`REC status failed: ${String(error)}`, 'error', undefined, error);
				return;
			}
			unreachableReads += 1;
			statusRetry = { after: recorderStatusRetryMs(unreachableReads) };
			if (unreachableReads === REC_STATUS_UNREACHABLE_LIMIT) {
				pushToast(
					recorderStatusUnreachableMessage(unreachableReads, error),
					'error',
					undefined,
					error,
					{},
					REC_STATUS_UNREACHABLE_GROUP
				);
			}
		}
	}

	function mount(): void {
		if (recordable()) void refreshRecorderStatus();
	}

	async function openPicker(variant: 'modal' | 'popover'): Promise<void> {
		pickerVariant = variant;
		RecordInputPicker = await loadRecordInputPicker();
	}

	function closePicker(): void {
		RecordInputPicker = null;
	}

	async function togglePerformanceRecording(): Promise<void> {
		recorderBusy = true;
		try {
			recorder = await getRecorderStatus();
			if (recorder.active) {
				if (!recorder.owned) {
					return;
				}
				stopping = true;
				try {
					await stopMasterTap();
					setRecorder(await stopPerformanceRecorder(recorder));
				} finally {
					stopping = false;
				}
				pushToast('Recording stopped and session finalized.', 'info');
				closePicker();
				return;
			}
			await openPicker('modal');
		} catch (error) {
			pushToast(`REC failed: ${String(error)}`, 'error');
		} finally {
			recorderBusy = false;
		}
	}

	async function openRecordPanel(): Promise<void> {
		if (recorderBusy) return;
		recorderBusy = true;
		try {
			recorder = await getRecorderStatus();
			if (recorder.active) {
				if (!recorder.owned) {
					return;
				}
				stopping = true;
				try {
					await stopMasterTap();
					setRecorder(await stopPerformanceRecorder(recorder));
				} finally {
					stopping = false;
				}
				pushToast('Recording stopped and session finalized.', 'info');
				closePicker();
				return;
			}
			await openPicker('popover');
		} catch (error) {
			pushToast(`REC failed: ${String(error)}`, 'error');
		} finally {
			recorderBusy = false;
		}
	}

	function onPickerStarted(status: RecorderStatus): void {
		setRecorder(status);
		closePicker();
	}

	return {
		mount,
		get recorder() {
			return recorder;
		},
		get rail() {
			return rail;
		},
		get recorderBusy() {
			return recorderBusy;
		},
		get RecordInputPicker() {
			return RecordInputPicker;
		},
		get pickerVariant() {
			return pickerVariant;
		},
		refreshRecorderStatus,
		togglePerformanceRecording,
		openRecordPanel,
		closePicker,
		onPickerStarted
	};
}
