<script lang="ts">
	/** Storybook-only: mirrors the app's armed flag so arm clicks show in the control. */
	import type { SafetyLoopSlot } from '$lib/rb/deck-state-types';
	import type { DeckId } from '$lib/rb/performance-hotkeys';

	import LoopSafetyControls from './LoopSafetyControls.svelte';

	let {
		deckId,
		pending,
		safety,
		canSaveSafety,
		safetyTip,
		onSafetySave,
		onSafetyArm,
		onSafetyClear
	}: {
		deckId: DeckId;
		pending: boolean;
		safety: SafetyLoopSlot | null;
		canSaveSafety: boolean;
		safetyTip: string;
		onSafetySave: () => Promise<void>;
		onSafetyArm: (armed: boolean) => Promise<void>;
		onSafetyClear: () => Promise<void>;
	} = $props();

	let armed: boolean | null = $state(null);
	const shown = $derived(safety === null ? null : { ...safety, armed: armed ?? safety.armed });
</script>

<LoopSafetyControls
	{deckId}
	{pending}
	safety={shown}
	{canSaveSafety}
	{safetyTip}
	{onSafetySave}
	onSafetyArm={async (next) => {
		armed = next;
		await onSafetyArm(next);
	}}
	{onSafetyClear}
/>
