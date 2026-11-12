<script lang="ts">
	// Pad FX 1 / Pad FX 2 bank (PADFX-11718): eight hold-to-apply presets from
	// pad-fx-presets.json in the hot-cue bank's 2x4 layout (A-D left, E-H right).
	// Pointer down engages, pointer up, cancel or leave lets go (no pointer
	// capture, so leave fires); arrows while held step the beats; Space/Enter on
	// a focused pad press and release it the same way. Commands come from the
	// pure controller (pad-fx-mode.ts) and go out through the beat FX unit port;
	// with no unit the pads are disabled with the port's reason. Mimicked:
	// rekordbox 7.2.8 manual p. 128 and pp. 182-183.
	import type { DeckId } from '$lib/rb/deck-id';
	import type { BeatFxDeckTarget } from '$lib/rb/beat-fx';
	import {
		padFxBank,
		padFxPresetReadiness,
		type PadFxModeId,
		type PadFxPreset
	} from '$lib/rb/pad-fx-presets';
	import {
		createPadFxController,
		type PadFxCommand,
		type PadFxController
	} from '$lib/rb/pad-fx-mode';
	import { beatFxUnitAvailability, sendBeatFxUnitCommand } from '$lib/rb/beat-fx-unit-port.svelte';
	import { pushToast } from '$lib/stores.svelte';

	let { deckId, mode }: { deckId: DeckId; mode: PadFxModeId } = $props();

	const target = $derived(String(deckId) as BeatFxDeckTarget);
	const bank = $derived(padFxBank(mode));
	const unit = $derived(beatFxUnitAvailability(target));
	let controller: PadFxController | null = null;
	let active: number | null = $state(null);
	let heldPointer: Record<number, number | 'key'> = $state({});

	function send(commands: PadFxCommand[]): void {
		try {
			for (const command of commands) sendBeatFxUnitCommand(target, command);
		} catch (err) {
			pushToast(err instanceof Error ? err.message : String(err), 'error');
			throw err;
		}
	}

	function sync(): void {
		active = controller?.state().active ?? null;
	}

	$effect(() => {
		const c = createPadFxController(padFxBank(mode), String(deckId) as BeatFxDeckTarget);
		controller = c;
		heldPointer = {};
		sync();
		return () => {
			const out = c.releaseAll();
			if (out.length > 0) send(out);
		};
	});

	function disabledReason(preset: PadFxPreset): string | null {
		if (!unit.enabled) return unit.reason;
		const ready = padFxPresetReadiness(preset);
		return ready.live ? null : ready.reason;
	}

	function liveTitle(preset: PadFxPreset): string {
		const how = preset.kind === 'release' && !preset.hold ? 'press to toggle' : 'hold to apply';
		return `${preset.label} - ${how}`;
	}

	function engage(i: number, holder: number | 'key'): void {
		if (controller === null || heldPointer[i] !== undefined) return;
		heldPointer[i] = holder;
		send(controller.press(i));
		sync();
	}

	function letGo(i: number, holder: number | 'key'): void {
		if (controller === null || heldPointer[i] !== holder) return;
		delete heldPointer[i];
		send(controller.release(i));
		sync();
	}

	function onWindowKeyDown(e: KeyboardEvent): void {
		if (controller === null || Object.keys(heldPointer).length === 0) return;
		if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
		e.preventDefault();
		send(controller.step(e.key === 'ArrowLeft' ? -1 : 1));
	}

	function onPadKeyDown(e: KeyboardEvent, i: number): void {
		if (e.key !== ' ' && e.key !== 'Enter') return;
		e.preventDefault();
		if (!e.repeat) engage(i, 'key');
	}

	function onPadKeyUp(e: KeyboardEvent, i: number): void {
		if (e.key !== ' ' && e.key !== 'Enter') return;
		e.preventDefault();
		letGo(i, 'key');
	}

	const columns = $derived([
		bank.slice(0, 4).map((p, k) => ({ p, i: k })),
		bank.slice(4, 8).map((p, k) => ({ p, i: k + 4 }))
	]);
</script>

<svelte:window onkeydown={onWindowKeyDown} />

<div
	class="pad-fx-bank"
	role="group"
	aria-label={`pad fx pads deck ${deckId}`}
	data-pad-fx-mode={mode}
>
	{#each columns as column, c (c)}
		<div class="pad-fx-col">
			{#each column as { p, i } (i)}
				{@const reason = disabledReason(p)}
				<button
					type="button"
					class="rb-lit-button pad-fx-pad"
					class:active={active === i}
					class:rb-inert={reason !== null}
					data-testid={`pad-fx-deck-${deckId}-pad-${i}`}
					data-pad-fx-mode={mode}
					data-active={active === i}
					disabled={reason !== null}
					title={reason ?? liveTitle(p)}
					onpointerdown={(e) => engage(i, e.pointerId)}
					onpointerup={(e) => letGo(i, e.pointerId)}
					onpointercancel={(e) => letGo(i, e.pointerId)}
					onpointerleave={(e) => letGo(i, e.pointerId)}
					onkeydown={(e) => onPadKeyDown(e, i)}
					onkeyup={(e) => onPadKeyUp(e, i)}
				>
					{p.label}
				</button>
			{/each}
		</div>
	{/each}
</div>

<style>
	.pad-fx-bank {
		display: flex;
		gap: 3px;
		flex: 0 0 auto;
		min-height: 0;
	}
	.pad-fx-col {
		display: flex;
		flex-direction: column;
		gap: 3px;
		min-width: 0;
		flex: 1 1 50%;
	}
	.pad-fx-pad {
		height: 22px;
		font-size: 10px;
		min-width: 0;
		overflow: hidden;
		white-space: nowrap;
		text-overflow: ellipsis;
		touch-action: none;
	}
	.pad-fx-pad.active {
		color: var(--rb-text);
		border-color: var(--rb-text);
	}
	.pad-fx-pad.rb-inert {
		opacity: 0.55;
		cursor: default;
	}
</style>
