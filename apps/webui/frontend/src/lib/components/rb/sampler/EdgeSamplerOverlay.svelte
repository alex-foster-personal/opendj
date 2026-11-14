<script lang="ts">
	/**
	 * Both edge waves (spec 7.4). One rAF loop only while an edge is awake or easing (EDGESAMP-07).
	 * On the Electron shell it also starts the sampler store (catalogue load, master follow)
	 * and plugs the Web Audio engine into it for its own lifetime. While a deck loop is dragged
	 * in it draws the ghost under the pointer (mock `.ghost`). Its pointer samples mark the edge the
	 * pointer is heading for (`intent`), so the residency feed warms that page first (#10782).
	 */
	import { onMount } from 'svelte';
	import { prefersReducedMotion } from '$lib/motion/reduced-motion';
	import { OVERLAY_Z } from '$lib/overlays/stack';
	import { DEFAULT_WAVE, ZONE_PX } from '$lib/player/sampler/edge-wave';
	import { samplerEngine, stopSamplerVoices } from '$lib/player/sampler/sampler-engine.svelte';
	import {
		INTENT_HISTORY_POINTS,
		pointerIntent,
		type PointerPoint
	} from '$lib/player/sampler/sampler-residency';
	import { NO_ENGINE } from '$lib/player/sampler/sampler-engine-seam';
	import {
		edgeItems,
		registerSamplerEngine,
		sampler,
		slotPhase,
		startSampler
	} from '$lib/player/sampler/sampler-store.svelte';
	import { uiPrefs } from '$lib/rb/prefs.svelte';
	import { nativeShellKind } from '$lib/shell/native-shell';
	import EdgeWave, { type Pointer } from './EdgeWave.svelte';

	const TOP_PX = 28;
	const BOTTOM_PX = 20;

	const enabled = $derived(uiPrefs.sampler_enabled);
	const shellOk = $derived(typeof window !== 'undefined' && nativeShellKind() === 'electron');

	let innerH = $state(typeof window === 'undefined' ? 800 : window.innerHeight);
	const height = $derived(Math.max(200, innerH - TOP_PX - BOTTOM_PX));

	let waveL: EdgeWave | undefined = $state();
	let waveR: EdgeWave | undefined = $state();
	const waves = (): EdgeWave[] => [waveL, waveR].filter((w): w is EdgeWave => w !== undefined);

	const ptr: Pointer = { x: -1, y: -1, buttons: 0, inWindow: false };
	let raf = 0;
	let lastT = 0;
	const reduceMotion = typeof window !== 'undefined' && prefersReducedMotion();

	function frame(t: number): void {
		raf = 0;
		const dt = reduceMotion ? 1e6 : Math.min(64, t - (lastT || t));
		lastT = t;
		let busy = false;
		for (const w of waves()) busy = w.step(t, dt, ptr) || busy;
		if (busy) raf = requestAnimationFrame(frame);
		else lastT = 0;
	}

	function ensureLoop(): void {
		if (raf === 0) raf = requestAnimationFrame(frame);
	}

	/** The last INTENT_HISTORY_POINTS pointer samples, oldest first. */
	const trail: PointerPoint[] = [];

	function markIntent(ev: PointerEvent): void {
		trail.push({ t: ev.timeStamp, x: ev.clientX });
		if (trail.length > INTENT_HISTORY_POINTS) trail.shift();
		for (const edge of ['L', 'R'] as const) {
			const on = pointerIntent(trail, edge, window.innerWidth, ZONE_PX);
			if (sampler.edges[edge].intent !== on) sampler.edges[edge].intent = on;
		}
	}

	const nearEdge = (x: number): boolean => x <= ZONE_PX || window.innerWidth - x <= ZONE_PX;

	function onMove(ev: PointerEvent): void {
		const prevX = ptr.x;
		ptr.x = ev.clientX;
		ptr.y = ev.clientY;
		ptr.buttons = ev.buttons;
		ptr.inWindow = true;
		for (const w of waves()) w.onPointerMove(prevX, ptr);
		if (!enabled) return;
		markIntent(ev);
		let animating = false;
		for (const w of waves()) {
			if (!w.collapsed()) animating = true;
		}
		if (nearEdge(ev.clientX) || animating) ensureLoop();
	}

	function onDown(ev: PointerEvent): void {
		ptr.buttons = ev.buttons;
		for (const w of waves()) w.onPointerDown({ ...ptr, x: ev.clientX, y: ev.clientY });
	}

	function onUp(ev: PointerEvent): void {
		ptr.buttons = ev.buttons;
	}

	function onLeave(): void {
		ptr.inWindow = false;
		if (enabled) ensureLoop();
	}

	$effect(() => {
		void enabled;
		// A flip or a pin restarts the frame loop so the tabs pop or stay out.
		void sampler.edges.L.flipAt;
		void sampler.edges.R.flipAt;
		void sampler.pinned;
		// A loop drag starting or ending wakes or settles the edges.
		void (sampler.drag === null);
		// A voice arming, starting or stopping (slotPhase reads the engine's $state) holds or frees a tab.
		void sampler.engineReady;
		for (const edge of ['L', 'R'] as const)
			for (const s of edgeItems(edge)) if (s) void slotPhase(s.slot_id);
		ensureLoop();
	});

	onMount(() => {
		if (shellOk) registerSamplerEngine(samplerEngine);
		const stopSampler = shellOk ? startSampler() : null;
		const onResize = (): void => {
			innerH = window.innerHeight;
		};
		const docLeave = (ev: PointerEvent): void => {
			if (ev.relatedTarget === null) onLeave();
		};
		window.addEventListener('pointermove', onMove, { passive: true });
		window.addEventListener('pointerdown', onDown, { capture: true });
		window.addEventListener('pointerup', onUp, { passive: true });
		window.addEventListener('blur', onLeave);
		window.addEventListener('resize', onResize);
		document.addEventListener('pointerout', docLeave);
		return () => {
			window.removeEventListener('pointermove', onMove);
			window.removeEventListener('pointerdown', onDown, { capture: true });
			window.removeEventListener('pointerup', onUp);
			window.removeEventListener('blur', onLeave);
			window.removeEventListener('resize', onResize);
			document.removeEventListener('pointerout', docLeave);
			if (raf !== 0) cancelAnimationFrame(raf);
			stopSampler?.();
			if (shellOk) {
				stopSamplerVoices();
				registerSamplerEngine(NO_ENGINE);
			}
		};
	});
</script>

{#if shellOk}
	<div
		class="edge-sampler"
		class:off={!enabled}
		style:z-index={OVERLAY_Z.edgeSampler}
		data-testid="edge-sampler"
	>
		<EdgeWave
			bind:this={waveL}
			edge="L"
			top={TOP_PX}
			{height}
			params={DEFAULT_WAVE}
			{enabled}
			{reduceMotion}
		/>
		<EdgeWave
			bind:this={waveR}
			edge="R"
			top={TOP_PX}
			{height}
			params={DEFAULT_WAVE}
			{enabled}
			{reduceMotion}
		/>
		{#if sampler.drag !== null}
			<div
				class="ghost"
				style:left="{sampler.drag.x}px"
				style:top="{sampler.drag.y}px"
				data-testid="edge-sampler-drag-ghost"
			>
				<svg viewBox="0 0 16 16" aria-hidden="true"
					><path
						d="M3 7a5 5 0 018.5-3.5L13 5M13 2v3h-3M13 9a5 5 0 01-8.5 3.5L3 11M3 14v-3h3"
					/></svg
				>
				<span>Deck {sampler.drag.deck} loop . {sampler.drag.bars} . mix kept</span>
			</div>
		{/if}
	</div>
{/if}

<style>
	.edge-sampler {
		position: fixed;
		inset: 0;
		pointer-events: none;
	}
	.ghost {
		position: fixed;
		/* above the edge waves (z-index 340) */
		z-index: 360;
		pointer-events: none;
		white-space: nowrap;
		display: flex;
		gap: 6px;
		align-items: center;
		padding: 6px 10px;
		border-radius: 5px;
		background: var(--es-ghost-bg);
		border: 1px solid var(--es-accent);
		color: var(--es-fg);
		font: 12.5px var(--es-font-body);
		transform: translate(-50%, -120%);
		box-shadow: 0 8px 24px var(--es-shadow-ghost);
	}
	.ghost svg {
		width: 15px;
		height: 15px;
		stroke: var(--es-accent);
		fill: none;
		stroke-width: 1.7;
		stroke-linecap: round;
		stroke-linejoin: round;
	}
	.edge-sampler.off :global(.wslot:not(.held)),
	.edge-sampler.off :global(.whead) {
		display: none;
	}
</style>
