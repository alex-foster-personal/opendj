<script lang="ts">
	/**
	 * Schematic replica of a controller's top panel (ADR-0137). Flat shapes, our own type,
	 * no vendor marks, never a photo. Every control dispatches through the same action switch
	 * the real controller's MIDI drives (handleMidiAction), resolved from the same DeviceMap.
	 */
	import {
		initialPadLayers,
		selectPadLayer,
		queryPadLayers,
		type PadLayerCommand
	} from './practice-pad-layers';
	import type { DeviceMap } from '$lib/rb/midi/midi-types';
	import { onDestroy } from 'svelte';
	import { handleMidiAction } from '$lib/rb/midi/action-glue.svelte';
	import { midiState, setDeviceShiftHeld } from '$lib/rb/midi/midi-state.svelte';
	import {
		controlTitle,
		createPracticeHolds,
		dispatchPractice,
		resolveControl,
		resolveLayout,
		type PracticeSinks,
		type PanelLayout,
		type PadLayerSelection,
		type ResolvedControl
	} from './practice-bindings';

	interface Props {
		layout: PanelLayout;
		map: DeviceMap;
		/** Practice drill target control id; null = free play. */
		target?: string | null;
		onHit?: (controlId: string) => void;
		/** When true, the replica does not accept pointer input (compare / blind modes). */
		disablePointerEvents?: boolean;
	}

	let { layout, map, target = null, onHit, disablePointerEvents = false }: Props = $props();

	let selection = $state<PadLayerSelection>({});
	$effect.pre(() => {
		selection = initialPadLayers(layout);
	});
	const resolved = $derived(resolveLayout(map, layout, midiState.shiftHeld, selection));

	export function queryLayers() {
		return queryPadLayers(layout, map, selection, midiState.shiftHeld);
	}

	export function dispatchLayer(cmd: PadLayerCommand) {
		if (disablePointerEvents) throw new Error('Pad layer selection is disabled for this replica');
		const next = selectPadLayer(layout, selection, cmd);
		// Resolve first so an unknown map wire fails before the state is changed.
		resolveLayout(map, layout, midiState.shiftHeld, next);
		selection = next;
		return queryLayers();
	}

	function _select(r: ResolvedControl): void {
		const c = r.control;
		if (c.pad_layer_group === undefined || c.pad_layer_select === undefined)
			throw new Error(`missing pad selector metadata on ${c.id}`);
		dispatchLayer({
			type: 'select',
			group: c.pad_layer_group,
			mode: midiState.shiftHeld ? c.pad_layer_select.shift : c.pad_layer_select.base
		});
		_reach(r);
	}

	function _selectorTitle(r: ResolvedControl): string {
		const select = r.control.pad_layer_select;
		const group = layout.pad_layer_groups?.[r.control.pad_layer_group ?? ''];
		const mode = midiState.shiftHeld ? select?.shift : select?.base;
		const label = group?.modes.find((m) => m.id === mode)?.label;
		return `${controlTitle(r)}. Inspect ${label} pad wires; inspection only`;
	}

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
			setDeviceShiftHeld('practice-replica', held);
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
		if (disablePointerEvents) return;
		// Resolve at press time: a held SHIFT changes what the control sends, as on hardware.
		dispatchPractice(
			resolveControl(map, r.control, midiState.shiftHeld, selection, layout),
			input,
			SINKS
		);
	}

	// Buttons held right now, by control id, with the layer each was pressed on, so the release
	// goes to the same action even if SHIFT changed in between, and fires exactly once.
	const _held = createPracticeHolds(SINKS);

	function _press(r: ResolvedControl, e: PointerEvent): void {
		if (disablePointerEvents) return;
		const live = resolveControl(map, r.control, midiState.shiftHeld, selection, layout);
		if (live.kind !== 'bound') return;
		(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
		_reach(r);
		_held.press(live);
	}

	/** pointerup, pointercancel and lostpointercapture all land here; only the first releases. */
	function _release(r: ResolvedControl): void {
		_held.release(r.control.id);
	}

	// Leaving the page must not strand a held SHIFT in the global modifier state WebMIDI reads.
	onDestroy(() => {
		_held.releaseAll();
	});

	function _isContinuous(r: ResolvedControl): boolean {
		return ['fader', 'knob', 'crossfader'].includes(r.control.type);
	}

	function _isVertical(r: ResolvedControl): boolean {
		return r.control.type === 'fader';
	}
</script>

{#if layout.pad_layer_groups !== undefined}
	<div class="pad-layers" hidden={disablePointerEvents}>
		{#each Object.entries(layout.pad_layer_groups) as [id, group] (id)}
			<label>
				{group.label}
				<select
					aria-label={group.label}
					value={selection[id]}
					disabled={disablePointerEvents}
					onchange={(e) =>
						dispatchLayer({ type: 'select', group: id, mode: e.currentTarget.value })}
				>
					{#each group.modes as mode (mode.id)}
						<option value={mode.id}>{mode.label}</option>
					{/each}
				</select>
			</label>
		{/each}
		<span>Inspect pad wires. Unsupported actions remain unavailable.</span>
	</div>
{/if}
<div
	class="replica"
	class:replica-no-input={disablePointerEvents}
	style="aspect-ratio:{layout.panel.w} / {layout.panel.d}"
	data-testid="device-replica"
	data-device={layout.device}
>
	{#each resolved as r (r.control.id)}
		<div
			class="ctl ctl-{r.control.type}"
			class:inert={r.kind !== 'bound'}
			class:target={target === r.control.id}
			style={_box(r)}
			title={controlTitle(r)}
			data-control-id={r.control.id}
			data-pad-layer={r.control.pad_layer_group ? selection[r.control.pad_layer_group] : undefined}
			data-wire={r.wire ? `ch ${r.wire.ch} ${r.wire.kind} ${r.wire.id}` : 'unavailable'}
			data-status={r.kind}
		>
			{#if r.control.pad_layer_select !== undefined}
				<button
					type="button"
					class="press"
					disabled={disablePointerEvents}
					title={_selectorTitle(r)}
					aria-label={_selectorTitle(r)}
					aria-pressed={selection[r.control.pad_layer_group ?? ''] ===
						(midiState.shiftHeld
							? r.control.pad_layer_select.shift
							: r.control.pad_layer_select.base)}
					onclick={() => _select(r)}>{r.control.label}</button
				>
			{:else if r.kind !== 'bound' && r.control.pad_layers === undefined}
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
				<!-- Keep pad capture targets mounted across layer changes so held bindings can release. -->
				<button
					type="button"
					class="press"
					title={controlTitle(r)}
					aria-disabled={r.kind !== 'bound'}
					tabindex={r.kind === 'bound' ? 0 : -1}
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
	.pad-layers {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 12px;
		margin-bottom: 12px;
		font-size: 12px;
	}
	.pad-layers[hidden] {
		display: none;
	}
	.pad-layers label {
		display: flex;
		align-items: center;
		gap: 8px;
	}
	.pad-layers select {
		color: #e8ebf0;
		background: #2b2f36;
		padding: 6px;
		border: 1px solid #7a8291;
	}
	.pad-layers span {
		color: #a9b0bc;
	}
	.replica-no-input,
	.replica-no-input * {
		pointer-events: none;
	}
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
