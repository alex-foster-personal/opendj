<script lang="ts">
	// Safety-loop controls, extracted out of LoopCluster.svelte: the SAVE /
	// arm-toggle / clear trio for the one safety-loop slot is a self-contained
	// concern from the rest of the cluster (readout, halve/double, interval
	// grid), sharing only `pending` and the deck-command noteLoopInteraction
	// convention.
	import type { SafetyLoopSlot } from '$lib/rb/deck-state-types';
	import { noteLoopInteraction, type DeckId } from '$lib/rb/performance-hotkeys';

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
		/** Capture the engaged loop into the one safety slot (arms it). */
		onSafetySave: () => Promise<void>;
		/** Arm or disarm the saved safety loop. */
		onSafetyArm: (armed: boolean) => Promise<void>;
		/** Drop the saved safety loop entirely. */
		onSafetyClear: () => Promise<void>;
	} = $props();

	async function saveSafety(): Promise<void> {
		if (!canSaveSafety) return;
		noteLoopInteraction(deckId);
		await onSafetySave();
	}

	async function toggleSafetyArmed(): Promise<void> {
		if (pending || safety === null) return;
		noteLoopInteraction(deckId);
		await onSafetyArm(!safety.armed);
	}

	async function clearSafety(): Promise<void> {
		if (pending || safety === null) return;
		noteLoopInteraction(deckId);
		await onSafetyClear();
	}
</script>

<div class="safety">
	{#if safety === null}
		<button
			class="safety-btn"
			disabled={!canSaveSafety}
			data-performance-control="safety-loop-save"
			data-testid={`save-safety-loop-deck-${deckId}`}
			aria-label={`save safety loop deck ${deckId}`}
			title={safetyTip}
			onclick={saveSafety}
		>
			SAFE
		</button>
	{:else}
		<button
			class="safety-btn"
			class:armed={safety.armed}
			disabled={pending}
			data-performance-control="safety-loop-arm"
			data-testid={`safety-loop-deck-${deckId}`}
			aria-label={`safety loop deck ${deckId}`}
			data-state={safety.armed ? 'on' : 'off'}
			aria-pressed={safety.armed}
			title={`safety loop ${safety.beat_length ?? '?'} beats at ${Math.round(safety.in_ms)} ms - ${safety.armed ? "armed, engages when this loop's out is reached" : 'saved but disarmed'}`}
			onclick={toggleSafetyArmed}
		>
			SAFE {safety.beat_length ?? ''}
		</button>
		<button
			class="safety-clear"
			disabled={pending}
			data-performance-control="safety-loop-clear"
			title="clear the saved safety loop"
			aria-label={`clear safety loop deck ${deckId}`}
			data-testid={`clear-safety-loop-deck-${deckId}`}
			onclick={clearSafety}
		>
			&times;
		</button>
	{/if}
</div>

<style>
	.safety {
		display: flex;
		gap: 2px;
		width: 100%;
	}
	.safety button {
		background: var(--rb-panel-raised);
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		color: var(--rb-text);
		font-family: var(--rb-font);
		font-size: var(--rb-fs-label);
		padding: 1px 0;
		cursor: pointer;
	}
	.safety-btn {
		flex: 1;
		font-variant-numeric: tabular-nums;
	}
	.safety-btn.armed {
		border-color: var(--rb-orange);
		color: var(--rb-orange);
	}
	.safety-clear {
		flex: 0 0 16px;
	}
	.safety button:disabled {
		color: var(--rb-text-dim);
		cursor: default;
		opacity: 0.6;
	}
</style>
