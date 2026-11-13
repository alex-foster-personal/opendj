<script lang="ts">
	/**
	 * BEATFX-14 (issue #9785): the beat FX unit panel the top bar FX button opens, laid out
	 * like the DJM-A9 Beat FX section. All logic lives in `beat-fx-panel.ts`; every control
	 * sends a `beat_fx` command through `runPerformanceCommandFromUi` and the panel shows the
	 * unit only through `beatFxView()`.
	 */
	import { beatFxView } from '$lib/rb/beat-fx-live.svelte';
	import {
		XPAD_IDLE,
		beatFxBeatStepCommand,
		beatFxChannelLamps,
		beatFxDivisionLabel,
		beatFxEffectCommand,
		beatFxEffectOptions,
		beatFxLevelCommand,
		beatFxOnOffPress,
		beatFxQuantizeCommand,
		beatFxQuantizeLamp,
		beatFxTargetCommand,
		beatFxTimeReadout,
		beatFxXpadDown,
		beatFxXpadMove,
		beatFxXpadPosition,
		beatFxXpadUp,
		runBeatFxPanelCommand,
		type BeatFxXpadStep
	} from '$lib/rb/beat-fx-panel';
	import type { BeatFxCommand, BeatFxEffect } from '$lib/rb/beat-fx';
	import { runPerformanceCommandFromUi } from '$lib/rb/performance-ipc.svelte';

	let { style = '', onclose }: { style?: string; onclose: () => void } = $props();

	const view = $derived(beatFxView());
	const quantizeLamp = $derived(beatFxQuantizeLamp(view));
	const effects = beatFxEffectOptions();
	let refusal = $state<string | null>(null);
	let touch = $state(XPAD_IDLE);

	async function send(command: BeatFxCommand | null): Promise<void> {
		refusal = await runBeatFxPanelCommand(runPerformanceCommandFromUi, command);
	}

	function step(next: BeatFxXpadStep): void {
		touch = next.touch;
		void send(next.command);
	}

	function padX(event: PointerEvent): number {
		const rect = (event.currentTarget as HTMLElement).getBoundingClientRect();
		return beatFxXpadPosition(event.clientX, rect.left, rect.width);
	}
</script>

<div
	class="beat-fx-panel"
	role="dialog"
	aria-label="Beat FX"
	tabindex="-1"
	{style}
	onkeydown={(e) => {
		if (e.key === 'Escape') onclose();
	}}
>
	<div class="row">
		<span class="dim">BEAT FX</span>
		<span class="dim">{view.tempo_bpm === null ? '--' : view.tempo_bpm.toFixed(1)} BPM</span>
		<button type="button" class="close" aria-label="Close beat FX" onclick={onclose}>x</button>
	</div>
	<div class="row" role="group" aria-label="CH SELECT">
		{#each beatFxChannelLamps(view) as lamp (lamp.target)}
			<button
				type="button"
				class:lit={lamp.lit}
				class:blink={lamp.blinking}
				aria-pressed={lamp.lit}
				onclick={() => send(beatFxTargetCommand(lamp.target))}>{lamp.label}</button
			>
		{/each}
	</div>
	<select
		aria-label="Beat FX effect"
		value={view.effect}
		onchange={(e) =>
			send(beatFxEffectCommand((e.currentTarget as HTMLSelectElement).value as BeatFxEffect))}
	>
		{#each effects as effect (effect.name)}
			<option value={effect.name} disabled={!effect.built}>{effect.name.toUpperCase()}</option>
		{/each}
	</select>
	<div class="row" role="group" aria-label="BEAT">
		<button
			type="button"
			aria-label="Beat back"
			onclick={() => send(beatFxBeatStepCommand(view.beats, -1))}>&lt;</button
		>
		<span>{beatFxDivisionLabel(view.beats)}</span>
		<span class="dim">{beatFxTimeReadout(view)}</span>
		<button
			type="button"
			aria-label="Beat forward"
			onclick={() => send(beatFxBeatStepCommand(view.beats, 1))}>&gt;</button
		>
	</div>
	<div
		class="xpad"
		role="slider"
		tabindex="-1"
		aria-label="X-PAD"
		aria-valuenow={view.beats}
		class:touching={touch.touching}
		onpointerdown={(e) => {
			(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
			step(beatFxXpadDown(view, padX(e)));
		}}
		onpointermove={(e) => step(beatFxXpadMove(touch, padX(e)))}
		onpointerup={() => step(beatFxXpadUp(touch))}
		onpointercancel={() => step(beatFxXpadUp(touch))}
	>
		X-PAD
	</div>
	<label class="row">
		<span class="dim">LEVEL/DEPTH</span>
		<input
			type="range"
			min="0"
			max="1"
			step="0.01"
			value={view.level}
			oninput={(e) => send(beatFxLevelCommand(Number((e.currentTarget as HTMLInputElement).value)))}
		/>
		<span class="dim">{Math.round(view.level * 100)}%</span>
	</label>
	<div class="row">
		<button
			type="button"
			aria-pressed={view.quantize}
			onclick={() => send(beatFxQuantizeCommand(view))}
			>QUANTIZE{#if quantizeLamp !== 'hidden'}<span
					class="lamp"
					class:gray={quantizeLamp === 'gray'}
				></span>{/if}</button
		>
		<button
			type="button"
			class:engaged={view.enabled}
			class:blink={view.enabled}
			aria-pressed={view.enabled}
			onclick={() => step(beatFxOnOffPress(view, touch))}>ON/OFF</button
		>
	</div>
	{#if refusal !== null}
		<p class="refusal" role="alert">{refusal}</p>
	{/if}
</div>

<style>
	.beat-fx-panel {
		position: fixed;
		z-index: 60;
		display: flex;
		flex-direction: column;
		gap: 6px;
		min-width: 228px;
		padding: 8px;
		background: var(--rb-panel);
		border: 1px solid var(--rb-border);
		border-radius: 3px;
		box-shadow: 0 6px 18px rgb(0 0 0 / 0.45);
		color: var(--rb-text);
		font: 10px var(--rb-font);
	}
	.row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 4px;
	}
	.dim {
		color: var(--rb-text-dim);
		font-variant-numeric: tabular-nums;
	}
	button,
	select {
		background: transparent;
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		color: var(--rb-text-dim);
		font: inherit;
		padding: 3px 6px;
		cursor: pointer;
	}
	.lit {
		color: var(--rb-accent);
		border-color: var(--rb-accent);
	}
	.engaged {
		color: var(--rb-on-accent);
		background: var(--rb-accent);
	}
	.blink {
		animation: beat-fx-blink 1s steps(2, start) infinite;
	}
	@keyframes beat-fx-blink {
		to {
			opacity: 0.45;
		}
	}
	:global(html[data-reduce-motion]) .blink {
		animation: none;
	}
	@media (prefers-reduced-motion: reduce) {
		.blink {
			animation: none;
		}
	}
	.close {
		border: none;
	}
	.lamp {
		display: inline-block;
		width: 6px;
		height: 6px;
		margin-left: 4px;
		border-radius: 50%;
		background: var(--rb-accent);
	}
	.lamp.gray {
		background: var(--rb-text-dim);
	}
	.xpad {
		height: 28px;
		display: flex;
		align-items: center;
		justify-content: center;
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		color: var(--rb-text-dim);
		letter-spacing: 0.08em;
		touch-action: none;
		user-select: none;
	}
	.xpad.touching {
		border-color: var(--rb-accent);
		color: var(--rb-accent);
	}
	.refusal {
		margin: 0;
		color: var(--rb-red);
	}
</style>
