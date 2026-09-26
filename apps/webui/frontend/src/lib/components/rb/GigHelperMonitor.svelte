<script lang="ts">
	import { onGigHelperPressureSnapshot } from '$lib/rb/gig-helper-pressure-toast';
	import { subscribeMachinePressure } from '$lib/rb/machine-pressure';
	import { uiPrefs } from '$lib/rb/prefs.svelte';
	import { pushToast } from '$lib/stores.svelte';

	const active = $derived(uiPrefs.gig_helper === 'on' && uiPrefs.app_posture === 'gig');

	$effect(() => {
		if (!active) return;
		return subscribeMachinePressure((snapshot) => {
			onGigHelperPressureSnapshot(snapshot, {
				pushToast: (message, kind) => pushToast(message, kind)
			});
		});
	});
</script>
