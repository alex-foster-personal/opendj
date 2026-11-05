<script lang="ts">
	/**
	 * Schematic replica of a controller's top panel (ADR-0137). Flat shapes, our own type,
	 * no vendor marks, never a photo. Every control dispatches through the same action switch
	 * the real controller's MIDI drives (handleMidiAction), resolved from the same DeviceMap.
	 */
	import type { DeviceMap } from '$lib/rb/midi/midi-types';
	import { onDestroy } from 'svelte';
	import { handleMidiAction } from '$lib/rb/midi/action-glue.svelte';
	import { midiState } from '$lib/rb/midi/midi-state.svelte';
	import {
		controlTitle,
		dispatchPractice,
		resolveControl,
		resolveLayout,
		type PracticeSinks,
		type PanelLayout,
		type ResolvedControl
	} from './practice-bindings';

	interface Props {
		layout: PanelLayout;
		map: DeviceMap;
		/** Practice drill target control id; null = free play. */
		target?: string | null;
		onHit?: (controlId: string) => void;
	}

	let { layout, map, target = null, onHit }: Props = $props();

	const resolved = $derived(resolveLayout(map, layout));

	function _box(r: ResolvedControl): string {
		const c = r.control;
		const left = (c.x / layout.panel.w) * 100;
		const top = (c.y / layout.panel.d) * 100;
		const width = (c.w / layout.panel.w) * 100;
		const height = (c.h / layout.panel.d) * 100;
		return `left:${left}%;top:${top}%;width:${width}%;height:${height}%`;
	}

	const SINKS: PracticeSinks = {
		action: (action, value, controlId) =>
			handleMidiAction(action, value, 'practice-replica', performance.now(), controlId),
		// The same write the hardware SHIFT makes in webmidi.svelte.ts.
		shift: (held) => {
			midiState.shiftHeld = held;
		},
		// One line per dispatch so an agent or a reviewer can see exactly what fired.
		log: (line) => console.info(line)
	};

	/** One reach per gesture: a press (not its release), or the start of a slider drag. */
	function _reach(r: ResolvedControl): void {
		onHit?.(r.control.id);
	}

	function _fire(
		r: ResolvedControl,
		input: { pressed: boolean } | { value01: number } | { delta: number }
	): void {
		// Resolve at press time: a held SHIFT changes what the control sends, as on hardware.
		dispatchPractice(resolveControl(map, r.control, midiState.shiftHeld), input, SINKS);
	}

	// Buttons held right now, by control id, with the layer each was pressed on, so the release
	// goes to the same action even if SHIFT changed in between, and fires exactly once.
	const _held = new Map<string, ResolvedControl>();

	function _press(r: ResolvedControl, e: PointerEvent): void {
		(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
		_reach(r);
		const live = resolveControl(map, r.control, midiState.shiftHeld);
		_held.set(r.control.id, live);
		dispatchPractice(live, { pressed: true }, SINKS);
	}

	/** pointerup, pointercancel and lostpointercapture all land here; only the first releases. */
	function _release(r: ResolvedControl): void {
		const live = _held.get(r.control.id);
		if (live === undefined) return;
		_held.delete(r.control.id);
		dispatchPractice(live, { pressed: false }, SINKS);
	}

	// Leaving the page must not strand a held SHIFT in the global modifier state WebMIDI reads.
	onDestroy(() => {
		for (const live of [..._held.values()]) _release(live);
	});

	function _isContinuous(r: ResolvedControl): boolean {
		return ['fader', 'knob', 'crossfader'].includes(r.control.type);
	}

	function _isVertical(r: ResolvedControl): boolean {
		return r.control.type === 'fader';
	}
</script>

<div
	class="replica"
	style="aspect-ratio:{layout.panel.w} / {layout.panel.d}"
	data-testid="device-replica"
	data-device={layout.device}
>
	{#each resolved as r (r.control.id)}
		<div
			class="ctl ctl-{r.control.type}"
			class:inert={r.kind === 'inert'}
			class:target={target === r.control.id}
			style={_box(r)}
			title={controlTitle(r)}
			data-control-id={r.control.id}
		>
			{#if r.kind === 'inert'}
				<span class="lbl" aria-disabled="true">{r.control.label}</span>
			{:else if _isContinuous(r)}
				<input
					type="range"
					min="0"
					max="1"
					step="0.01"
					value="0.5"
					class:vertical={_isVertical(r)}
					onpointerdown={() => _reach(r)}
					aria-label={controlTitle(r)}
					title={controlTitle(r)}
					oninput={(e) =>
						_fire(r, { value01: Number((e.currentTarget as HTMLInputElement).value) })}
				/>
				<span class="lbl">{r.control.label}</span>
			{:else if r.control.type === 'encoder'}
				<button
					type="button"
					title="{controlTitle(r)} (one step back)"
					onclick={() => {
						_reach(r);
						_fire(r, { delta: -1 });
					}}>-</button
				>
				<button
					type="button"
					title="{controlTitle(r)} (one step on)"
					onclick={() => {
						_reach(r);
						_fire(r, { delta: 1 });
					}}>+</button
				>
				<span class="lbl">{r.control.label}</span>
			{:else}
				<button
					type="button"
					class="press"
					title={controlTitle(r)}
					onpointerdown={(e) => _press(r, e)}
					onpointerup={() => _release(r)}
					onpointercancel={() => _release(r)}
					onlostpointercapture={() => _release(r)}>{r.control.label}</button
				>
			{/if}
		</div>
	{/each}
</div>

<style>
	.replica {
		--p-panel: #1a1c20;
		--p-control: #2b2f36;
		--p-edge: #7a8291;
		--p-label: #e8ebf0;
		--p-inert: #a9b0bc;
		--p-accent: #4da3ff;
		position: relative;
		width: 100%;
		background: var(--p-panel);
		border: 1px solid var(--p-edge);
		border-radius: 6px;
		font:
			600 10px/1.1 system-ui,
			sans-serif;
		color: var(--p-label);
	}
	.ctl {
		position: absolute;
		box-sizing: border-box;
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 2px;
		flex-direction: column;
		background: var(--p-control);
		border: 1px solid var(--p-edge);
		border-radius: 3px;
	}
	.ctl-jog,
	.ctl-knob,
	.ctl-encoder {
		border-radius: 50%;
	}
	.ctl.inert {
		color: var(--p-inert);
		border-style: dashed;
	}
	.ctl.target {
		outline: 3px solid var(--p-accent);
		outline-offset: 2px;
	}
	.press,
	.ctl button {
		all: unset;
		cursor: pointer;
		width: 100%;
		height: 100%;
		text-align: center;
		color: var(--p-label);
	}
	.ctl-encoder button {
		height: auto;
	}
	.ctl input[type='range'] {
		width: 90%;
		accent-color: var(--p-accent);
	}
	.ctl input.vertical {
		writing-mode: vertical-lr;
		direction: rtl;
		width: auto;
		height: 80%;
	}
	.lbl {
		pointer-events: none;
		font-size: 8px;
		text-align: center;
	}
</style>
