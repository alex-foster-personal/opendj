<script lang="ts">
	import { cancelQuit, confirmQuit } from '$lib/shell/quit-gate';
	import { isQuitConfirmOpen, subscribeQuitConfirmOpen } from '$lib/shell/quit-gate-state';
	import QuitConfirmOverlayView from './QuitConfirmOverlayView.svelte';

	let open = $state(false);

	$effect(() => {
		const sync = (): void => {
			open = isQuitConfirmOpen();
		};
		sync();
		return subscribeQuitConfirmOpen(sync);
	});
</script>

<QuitConfirmOverlayView {open} onCancel={() => cancelQuit()} onConfirm={() => void confirmQuit()} />
