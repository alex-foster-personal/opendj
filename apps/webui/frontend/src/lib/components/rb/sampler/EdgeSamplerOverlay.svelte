<script lang="ts">
	/**
	 * Both edge waves (spec 7.4). One rAF loop only while an edge is awake or easing (EDGESAMP-07).
	 */
	import { onMount } from 'svelte';
	import { prefersReducedMotion } from '$lib/motion/reduced-motion';
	import { OVERLAY_Z } from '$lib/overlays/stack';
	import { DEFAULT_WAVE, ZONE_PX } from '$lib/player/sampler/edge-wave';
	import { edgeSamplerUi } from '$lib/player/sampler/edge-sampler-ui.svelte';
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

	const nearEdge = (x: number): boolean => x <= ZONE_PX || window.innerWidth - x <= ZONE_PX;

	function onMove(ev: PointerEvent): void {
		const prevX = ptr.x;
		ptr.x = ev.clientX;
		ptr.y = ev.clientY;
		ptr.buttons = ev.buttons;
		ptr.inWindow = true;
		for (const w of waves()) w.onPointerMove(prevX, ptr);
		if (!enabled) return;
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
		void edgeSamplerUi.animBump;
		ensureLoop();
	});

	onMount(() => {
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
	</div>
{/if}

<style>
	.edge-sampler {
		position: fixed;
		inset: 0;
		pointer-events: none;
	}
	.edge-sampler.off :global(.wslot:not(.held)),
	.edge-sampler.off :global(.whead) {
		display: none;
	}
</style>
