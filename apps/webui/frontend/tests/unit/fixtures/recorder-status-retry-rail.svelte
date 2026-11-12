<script lang="ts">
	/** SET-13 mount bundle: retry effects at component scope (chrome lives in .svelte.ts). */
	import { onMount } from 'svelte';
	import { getRecorderStatus, type RecorderStatus } from '../../../src/routes/sets/sets-api';
	import { recordRailState } from '$lib/sets/performance-recorder';
	import { pushToast } from '$lib/stores.svelte';
	import {
		REC_STATUS_UNREACHABLE_GROUP,
		REC_STATUS_UNREACHABLE_LIMIT,
		recorderStatusRetryMs,
		recorderStatusUnreachable,
		recorderStatusUnreachableMessage
	} from '$lib/sets/recorder-status-retry';

	let { recordable = true }: { recordable?: boolean } = $props();

	let recorder = $state<RecorderStatus>({
		active: false,
		session_id: null,
		pid: null,
		owned: false,
		recoverable: false,
		capture: 'none',
		capture_error: null
	});
	const rail = $derived(recordRailState(recorder));
	let statusRetry = $state<{ after: number } | null>(null);
	let unreachableReads = 0;

	onMount(() => {
		if (recordable) void refreshRecorderStatus();
	});

	$effect(() => {
		if (statusRetry !== null) return;
		const after = rail.poll;
		if (after === null || !recordable) return;
		const timer = setTimeout(() => void refreshRecorderStatus(), after);
		return () => clearTimeout(timer);
	});

	$effect(() => {
		const retry = statusRetry;
		if (retry === null || !recordable) return;
		const timer = setTimeout(() => refreshRecorderStatus(), retry.after);
		return () => clearTimeout(timer);
	});

	async function refreshRecorderStatus(): Promise<void> {
		if (!recordable) return;
		try {
			recorder = await getRecorderStatus();
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
</script>

<div data-testid="recorder-retry-rail"></div>
