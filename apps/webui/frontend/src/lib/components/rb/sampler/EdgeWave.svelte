<script lang="ts">
	/**
	 * One screen edge: top tab shell plus a column of numbered tabs (spec 7.4).
	 * An in-app deck loop drag (sampler.drag) wakes it with a button held; the tab under the
	 * pointer wears the dashed drop outline and the tab a drop saved wears the ring.
	 */
	import { onDestroy, untrack } from 'svelte';
	import {
		HEAD_H,
		REST_W,
		controlsReveal,
		createEdgeMotion,
		stepEdgeMotion,
		tabRef,
		titleReveal,
		updateArming,
		wheelFlip,
		type WaveParams
	} from '$lib/player/sampler/edge-wave';
	import {
		edgeItems,
		edgeRef,
		flipSet,
		isPinned,
		openSlotEditor,
		pinEdgeTabOnClick,
		sampler,
		slotMatchesEdge,
		slotPhase,
		slotPlayhead
	} from '$lib/player/sampler/sampler-store.svelte';
	import { cueLabel } from '$lib/player/sampler/sampler-store-logic';
	import { createTriggerGuard } from '$lib/player/sampler/sampler-trigger-guards';
	import { displayName, type Edge, type SamplerSlot } from '$lib/player/sampler/sampler-types';
	import EdgeCompatPop from './EdgeCompatPop.svelte';
	import EdgeTab from './EdgeTab.svelte';
	import EditSampleModal from './EditSampleModal.svelte';
	import EdgeWaveTopTab from './EdgeWaveTopTab.svelte';
	import SetNameDialog from './SetNameDialog.svelte';
	import { selectorNode } from '$lib/keyboard/selector-register';
	import { edgeSamplerPadSelectorId, edgeSamplerSelectorId } from './edge-sampler-selector-ids';
	import './edge-sampler-tokens.css';

	export interface Pointer {
		x: number;
		y: number;
		buttons: number;
		inWindow: boolean;
	}

	let {
		edge,
		top,
		height,
		params,
		enabled,
		reduceMotion
	}: {
		edge: Edge;
		top: number;
		height: number;
		params: WaveParams;
		enabled: boolean;
		reduceMotion: boolean;
	} = $props();

	/** Top of the pad column; the selector group starts here so a move down from the top bar never lands on it. */
	const PADS_TOP = HEAD_H + 4;
	/** Below this width a tab or top tab is at rest: no card, empty slots hidden (mock `_w < 10`). */
	const REST_CLASS_W = 10;
	/** Slots per edge, fixed for this component's life (motion arrays are sized once). */
	const tabCount = untrack(() => params.perSet);
	const motion = createEdgeMotion(tabCount);
	const armed = { value: true };
	const trigger = createTriggerGuard();
	/** The set whose name dialog is open (it stays on that set if the edge flips); null = closed. */
	let namingSetId: string | null = $state(null);
	const namingSet = $derived(sampler.sets.find((s) => s.id === namingSetId) ?? null);
	/** The top tab's "Compatible with master" popover is open (mock `POP.kind === 'compat'`). */
	let compatOpen = $state(false);
	let lastWheelFlip = -1e9;

	const tabH = $derived(Math.max(12, Math.floor((height - HEAD_H - 8) / tabCount)));
	const page = $derived(sampler.edges[edge].set);
	/** One entry per slot: the shown set's slot, or null for an empty slot. */
	const tabs = $derived(
		Array.from({ length: tabCount }, (_, i): SamplerSlot | null => edgeItems(edge)[i] ?? null)
	);
	/** Pinned, sounding or edited tabs stay popped. */
	const isHeld = (s: SamplerSlot | null): boolean =>
		s !== null &&
		(isPinned(s.slot_id) || slotPhase(s.slot_id) !== 'free' || sampler.editing === s.slot_id);
	/** Per tab: shown by this edge's query and filters (empty slots always count as matching). */
	const matches = $derived(tabs.map((s) => s === null || slotMatchesEdge(edge, s)));
	/** Per tab: another tab of this edge is in the edit modal, so this one settles (mock `.editing`). */
	const editingOther = $derived.by(() => {
		const id = sampler.editingEdge === edge ? sampler.editing : null;
		const here = id !== null && tabs.some((s) => s?.slot_id === id);
		return tabs.map((s) => here && s?.slot_id !== id);
	});
	/** The slot this edge's edit modal shows (the store's copy, so a save shows at once); null = closed. */
	const editSlot = $derived(
		sampler.editingEdge === edge
			? (sampler.slots.find((s) => s.slot_id === sampler.editing) ?? null)
			: null
	);
	/** The edited tab's row on this edge, or -1 when the agent opened a slot this edge does not show. */
	const editIndex = $derived(tabs.findIndex((s) => s !== null && s.slot_id === editSlot?.slot_id));
	const isDropTarget = (i: number): boolean =>
		sampler.drag?.over?.edge === edge && sampler.drag.over.index === i;
	const searchOpen = $derived(
		sampler.edges[edge].searchFocus || sampler.edges[edge].query.trim() !== ''
	);
	/** Hover tip (mock `d.title`): ref, title (and its source when renamed), cue label, description. */
	const tabTip = (ref: string, s: SamplerSlot): string =>
		`${ref}  ${displayName(s)}${s.name ? ` (from ${s.title})` : ''} . ${cueLabel(s)}${s.description ? `\n${s.description}` : ''}`;

	let root: HTMLDivElement | undefined = $state();
	let modal: EditSampleModal | undefined = $state();
	let headEl: HTMLDivElement | undefined = $state();
	const tabEls: Array<HTMLDivElement | undefined> = $state([]);
	let headW = $state(REST_W);
	let widths: number[] = $state(Array(tabCount).fill(REST_W));
	let awake = $state(false);

	const fromEdge = (x: number): number => (edge === 'R' ? window.innerWidth - x : x);

	export function onPointerMove(prev: number, p: Pointer): void {
		updateArming(armed, fromEdge(prev), fromEdge(p.x), p.buttons, sampler.drag !== null);
	}

	export function onPointerDown(p: Pointer): void {
		if (fromEdge(p.x) <= 14 && !awake) armed.value = false;
	}

	/** Returns true while this edge still needs animation frames. */
	export function step(t: number, dt: number, p: Pointer): boolean {
		const fe = fromEdge(p.x);
		// Store flips (arrows, wheel, keys, show all) stamp flipAt on the frame clock.
		motion.flipT = sampler.edges[edge].flipAt;
		const held = tabs.map(isHeld);
		const filled = tabs.map((s) => s !== null);

		const widthsIn = [...widths];
		stepEdgeMotion(motion, {
			enabled,
			awake: motion.awake,
			armed: armed.value,
			fromEdgePx: fe,
			pointerY: p.y,
			top,
			headH: HEAD_H,
			tabH,
			tabCount,
			buttons: p.buttons,
			inWindow: p.inWindow,
			t,
			appDrag: sampler.drag !== null,
			held,
			filled,
			matches,
			editingOther,
			searchOpen,
			hold:
				sampler.edges[edge].searchFocus || namingSet !== null || editSlot !== null || compatOpen,
			params,
			reduceMotion,
			dtMs: dt,
			headW,
			widths: widthsIn
		});

		awake = motion.awake;
		headW = motion.headW;
		widths = [...motion.widths];
		if (motion.awake) sampler.lastEdge = edge;

		const keysAwake = awake || held.some(Boolean);
		if (sampler.edges[edge].awake !== keysAwake) sampler.edges[edge].awake = keysAwake;
		root?.classList.toggle('awake', keysAwake);
		root?.classList.toggle('asleep', !keysAwake);
		if (headEl) {
			headEl.style.width = `${headW.toFixed(1)}px`;
			headEl.classList.toggle('rest', headW < REST_CLASS_W);
		}
		for (let i = 0; i < tabCount; i++) {
			const el = tabEls[i];
			if (!el) continue;
			const w = widths[i] ?? REST_W;
			el.style.width = `${w.toFixed(1)}px`;
			el.style.setProperty('--reveal', controlsReveal(w).toFixed(2));
			el.style.setProperty('--treveal', titleReveal(w).toFixed(2));
			el.classList.toggle('rest', w < REST_CLASS_W);
			const slot = tabs[i];
			if (slot) paintPlayhead(el, slot.slot_id);
		}
		const previewing = modal?.paint() ?? false;
		return motion.needsFrame || held.some(Boolean) || previewing;
	}

	/** Move a sounding tab's waveform playhead to its voice's place in the pass (transform only). */
	function paintPlayhead(el: HTMLDivElement, slotId: string): void {
		const f = slotPlayhead(slotId);
		const ph = f === null ? null : el.querySelector<HTMLElement>('.wv .ph');
		if (!ph || f === null) return;
		const travel = (ph.parentElement?.clientWidth ?? 0) - ph.offsetWidth;
		ph.style.transform = `translateX(${(f * travel).toFixed(1)}px)`;
	}

	/** The click guard every control inside tab `i` passes: readable, settled, not a bounce (EDGESAMP-11872g). */
	const guardFor = (i: number) => (): boolean => {
		const now = performance.now();
		return trigger.accept({
			key: tabs[i]?.slot_id ?? `empty-${i}`,
			widthPx: widths[i] ?? REST_W,
			msSinceWake: now - motion.activeSince,
			now
		});
	};

	function onWheel(ev: WheelEvent): void {
		if ((ev.target as Element).closest('.pop')) return; // popovers sit off the edge, as in the mock
		const delta = wheelFlip(motion.awake, ev.deltaY, performance.now(), lastWheelFlip);
		if (motion.awake) ev.preventDefault();
		if (delta === 0) return;
		lastWheelFlip = performance.now();
		flipSet(edge, delta);
	}

	function onTabClick(i: number): void {
		if (guardFor(i)())
			pinEdgeTabOnClick(edge, i, widths[i] ?? REST_W, performance.now() - motion.activeSince);
	}

	/** Right-click on the top tab names the shown set; search results are not a set. */
	function openNaming(): void {
		const st = sampler.edges[edge];
		if (st.results === null) namingSetId = sampler.sets[st.set]?.id ?? null;
		if (namingSetId !== null) compatOpen = false;
	}

	onDestroy(() => {
		sampler.edges[edge].awake = false;
	});

	export function collapsed(): boolean {
		return !awake && motion.enterT === 0 && motion.leaveT === 0;
	}
</script>

<div
	bind:this={root}
	class="edge-wave asleep {edge}"
	style:top="{top}px"
	style:height="{height}px"
	data-testid="edge-wave-{edge}"
	onwheel={onWheel}
>
	<EdgeWaveTopTab
		bind:el={headEl}
		{edge}
		onrename={openNaming}
		{compatOpen}
		oncompat={() => {
			compatOpen = !compatOpen;
			if (compatOpen) namingSetId = null;
		}}
	/>
	<div
		class="wpads"
		style:top="{PADS_TOP}px"
		use:selectorNode={{ id: edgeSamplerSelectorId(edge), kind: 'group', enabled }}
	>
		{#each tabs as slot, i (i)}
			{#if slot}
				<!-- svelte-ignore a11y_click_events_have_key_events -->
				<!-- svelte-ignore a11y_no_static_element_interactions -->
				<div
					bind:this={tabEls[i]}
					class="wslot rest"
					class:held={isHeld(slot)}
					class:pinned={isPinned(slot.slot_id)}
					class:armed={slotPhase(slot.slot_id) === 'armed'}
					class:playing={slotPhase(slot.slot_id) === 'playing'}
					class:previewing={slotPhase(slot.slot_id) === 'previewing'}
					class:editing={sampler.editing === slot.slot_id}
					class:dim={!matches[i]}
					class:droptarget={isDropTarget(i)}
					class:justdropped={sampler.justDropped === slot.slot_id}
					style:top="{i * tabH}px"
					style:height="{tabH - 3}px"
					style:--cue={slot.color}
					title={tabTip(edgeRef(edge, i), slot)}
					data-tab={i + 1}
					data-drop-edge={edge}
					data-drop-index={i}
					data-slot-id={slot.slot_id}
					onclick={() => onTabClick(i)}
					use:selectorNode={{ id: edgeSamplerPadSelectorId(edge, i), kind: 'leaf', enabled: true }}
				>
					<i class="edge"></i>
					<span class="num">{String(i + 1).padStart(2, '0')}</span>
					<EdgeTab {edge} {slot} guard={guardFor(i)} />
				</div>
			{:else}
				<div
					bind:this={tabEls[i]}
					class="wslot empty rest"
					class:droptarget={isDropTarget(i)}
					style:top="{i * tabH}px"
					style:height="{tabH - 3}px"
					title="{tabRef(page, i)}  empty slot"
					data-tab={i + 1}
					data-drop-edge={edge}
					data-drop-index={i}
					use:selectorNode={{ id: edgeSamplerPadSelectorId(edge, i), kind: 'leaf', enabled: true }}
				>
					<span class="num">{String(i + 1).padStart(2, '0')}</span>
					<span class="elbl">empty: drag a loop hot cue here</span>
				</div>
			{/if}
		{/each}
	</div>
	{#if compatOpen}
		<EdgeCompatPop {edge} onclose={() => (compatOpen = false)} />
	{/if}
	{#if namingSet}
		<SetNameDialog {edge} set={namingSet} onclose={() => (namingSetId = null)} />
	{/if}
	{#if editSlot}
		<EditSampleModal
			bind:this={modal}
			{edge}
			slot={editSlot}
			anchorY={HEAD_H + 4 + Math.max(0, editIndex) * tabH + tabH / 2}
			areaHeight={height}
			onclose={() => openSlotEditor(null)}
		/>
	{/if}
</div>

<style>
	.edge-wave {
		position: fixed;
		width: auto;
		max-width: 520px;
		pointer-events: none;
		z-index: 340;
	}
	.edge-wave.R {
		right: 0;
		align-items: flex-end;
	}
	.edge-wave.L {
		left: 0;
	}
	/* Zero-width pad column pinned to the screen edge: the pads keep their old
	 * offsets, and its rect is the edge line a selector move reaches. */
	.wpads {
		position: absolute;
		bottom: 0;
		width: 0;
	}
	.edge-wave.L :global(.whead),
	.edge-wave.L .wpads,
	.edge-wave.L .wslot {
		left: 0;
	}
	.edge-wave.R :global(.whead),
	.edge-wave.R .wpads,
	.edge-wave.R .wslot {
		right: 0;
	}
	/* Card shell shared with the top tab (mock `.wslot, .whead`). */
	.wslot,
	.edge-wave :global(.whead) {
		position: absolute;
		box-sizing: border-box;
		overflow: hidden;
		pointer-events: auto;
		border: 1px solid var(--es-tab-border);
		box-shadow: 0 0 22px var(--es-shadow-tab);
	}
	.wslot {
		background: var(--es-tab-bg);
		cursor: pointer;
	}
	.edge-wave.R .wslot,
	.edge-wave.R :global(.whead) {
		right: 0;
		border-right: 0;
		border-radius: 6px 0 0 6px;
	}
	.edge-wave.L .wslot,
	.edge-wave.L :global(.whead) {
		left: 0;
		border-left: 0;
		border-radius: 0 6px 6px 0;
	}
	.wslot:global(.rest),
	.edge-wave :global(.whead.rest) {
		background: transparent;
		border-color: transparent;
		box-shadow: none;
	}
	.edge {
		position: absolute;
		top: 0;
		bottom: 0;
		width: 6px;
		background: var(--cue);
	}
	.edge-wave.R .edge {
		right: 0;
	}
	.edge-wave.L .edge {
		left: 0;
	}
	.wslot:global(.rest) .edge {
		width: 3px;
		opacity: 0.5;
	}
	/* Tab states (mock section "Tab states"); playheads and the dimmed row live in EdgeTab. */
	.wslot.armed {
		border-color: var(--es-warn);
		animation: pulse 0.45s ease-in-out infinite alternate;
	}
	.wslot.playing {
		background: var(--es-playing-bg);
		border-color: var(--es-accent);
	}
	.wslot.playing .edge {
		box-shadow: 0 0 12px var(--es-accent);
	}
	.wslot.editing {
		outline: 2px solid var(--es-deck-a);
		outline-offset: -2px;
	}
	/* Loop drag (mock `.droptarget`, `.justdropped`); the glow is the MIX badge's blue. */
	.wslot.droptarget {
		outline: 2px dashed var(--es-accent);
		outline-offset: -2px;
	}
	.wslot.justdropped {
		box-shadow:
			0 0 0 2px var(--es-deck-a),
			0 0 18px var(--es-mix-border);
	}
	@keyframes pulse {
		from {
			box-shadow: 0 0 0 0 var(--es-armed-glow-off);
		}
		to {
			box-shadow: 0 0 0 4px var(--es-armed-glow);
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.wslot.armed {
			animation: none;
		}
	}
	.wslot.empty {
		border-style: dashed;
		border-color: var(--es-border-strong);
		background: var(--es-empty-bg);
		box-shadow: none;
		cursor: default;
	}
	.wslot.empty:global(.rest) {
		display: none;
	}
	.elbl {
		position: absolute;
		top: 50%;
		transform: translateY(-50%);
		font: 11px var(--es-font-mono);
		color: var(--es-dim);
		white-space: nowrap;
		opacity: var(--reveal, 0);
	}
	.edge-wave.R .elbl {
		left: 40px;
	}
	.edge-wave.L .elbl {
		right: 40px;
	}
	.num {
		position: absolute;
		top: 0;
		bottom: 0;
		z-index: 2;
		display: flex;
		align-items: center;
		justify-content: center;
		width: 26px;
		background: inherit;
		font: 12px var(--es-font-mono);
		color: var(--es-muted);
		opacity: 0;
		transition: opacity 0.12s;
	}
	.edge-wave.R .num {
		left: 0;
	}
	.edge-wave.L .num {
		right: 0;
	}
	.edge-wave:global(.awake) .num {
		opacity: 1;
	}
	@media (prefers-reduced-motion: reduce) {
		.num {
			transition-duration: 0.01ms;
		}
	}
	:global(html[data-reduce-motion]) .num {
		transition-duration: 0.01ms;
	}
</style>
