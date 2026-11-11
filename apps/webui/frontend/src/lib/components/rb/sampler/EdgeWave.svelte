<script lang="ts">
	/**
	 * One screen edge: top tab shell plus a column of numbered tabs (spec 7.4).
	 */
	import {
		AWAKE_W,
		HEAD_OPEN_W,
		REST_W,
		TABS_PER_PAGE,
		createEdgeMotion,
		stepEdgeMotion,
		updateArming,
		type WaveParams
	} from '$lib/player/sampler/edge-wave';
	import { edgeSamplerUi, isHeld, type EdgeId } from '$lib/player/sampler/edge-sampler-ui.svelte';
	import EdgeWaveTopTab from './EdgeWaveTopTab.svelte';

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
		edge: EdgeId;
		top: number;
		height: number;
		params: WaveParams;
		enabled: boolean;
		reduceMotion: boolean;
	} = $props();

	const HEAD_H = 44;
	const motion = createEdgeMotion(TABS_PER_PAGE);
	const armed = { value: true };

	const tabH = $derived(Math.max(12, Math.floor((height - HEAD_H - 8) / TABS_PER_PAGE)));
	const page = $derived(edgeSamplerUi.pageByEdge[edge]);
	const tabs = $derived(edgeSamplerUi.tabs[edge]);

	let root: HTMLDivElement | undefined = $state();
	let headEl: HTMLDivElement | undefined = $state();
	const tabEls: Array<HTMLDivElement | undefined> = $state([]);
	let headW = $state(REST_W);
	let widths: number[] = $state(Array(TABS_PER_PAGE).fill(REST_W));
	let awake = $state(false);

	const fromEdge = (x: number): number => (edge === 'R' ? window.innerWidth - x : x);

	export function onPointerMove(prev: number, p: Pointer): void {
		updateArming(armed, fromEdge(prev), fromEdge(p.x), p.buttons, false);
	}

	export function onPointerDown(p: Pointer): void {
		if (fromEdge(p.x) <= 14 && !awake) armed.value = false;
	}

	/** Returns true while this edge still needs animation frames. */
	export function step(t: number, dt: number, p: Pointer): boolean {
		const fe = fromEdge(p.x);
		const held = tabs.map((_, i) => isHeld(edge, i));
		const filled = tabs.map((t) => t.filled);

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
			tabCount: TABS_PER_PAGE,
			buttons: p.buttons,
			inWindow: p.inWindow,
			t,
			appDrag: false,
			held,
			filled,
			params,
			reduceMotion,
			dtMs: dt,
			headW,
			widths: widthsIn
		});

		awake = motion.awake;
		headW = motion.headW;
		widths = [...motion.widths];
		if (motion.awake) edgeSamplerUi.lastEdge = edge;

		root?.classList.toggle('awake', awake || held.some(Boolean));
		root?.classList.toggle('asleep', !(awake || held.some(Boolean)));
		headEl && (headEl.style.width = `${headW.toFixed(1)}px`);
		for (let i = 0; i < TABS_PER_PAGE; i++) {
			const el = tabEls[i];
			if (!el) continue;
			const w = widths[i] ?? REST_W;
			const show = tabs[i].filled || awake || held[i];
			el.style.width = `${w.toFixed(1)}px`;
			el.style.opacity = show ? '1' : '0';
			el.classList.toggle('rest', w < AWAKE_W + 2);
			el.classList.toggle('popped', w > AWAKE_W + 4);
		}
		return motion.needsFrame || held.some(Boolean);
	}

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
>
	<EdgeWaveTopTab bind:el={headEl} {edge} {page} />
	{#each tabs as tab, i (i)}
		<div
			bind:this={tabEls[i]}
			class="wslot"
			class:empty={!tab.filled}
			class:held={isHeld(edge, i)}
			style:top="{HEAD_H + 4 + i * tabH}px"
			style:height="{tabH}px"
			style:--cue={tab.color}
			data-tab={i + 1}
		>
			<span class="num">{String(i + 1).padStart(2, '0')}</span>
		</div>
	{/each}
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
	.edge-wave.L :global(.whead),
	.edge-wave.L .wslot {
		left: 0;
	}
	.edge-wave.R :global(.whead),
	.edge-wave.R .wslot {
		right: 0;
	}
	.wslot {
		position: absolute;
		display: flex;
		align-items: center;
		justify-content: flex-end;
		padding-right: 4px;
		box-sizing: border-box;
		background: color-mix(in srgb, var(--cue) 50%, transparent);
		border: 1px dashed transparent;
		color: var(--rb-text);
		font: 10px/1 var(--rb-font);
		overflow: hidden;
		pointer-events: auto;
	}
	.edge-wave.asleep .wslot:not(.held) {
		border-color: transparent;
	}
	.edge-wave.awake .wslot.empty {
		border-color: color-mix(in srgb, var(--cue) 80%, var(--rb-border));
		background: color-mix(in srgb, var(--cue) 22%, transparent);
	}
	.wslot.held {
		background: color-mix(in srgb, var(--cue) 65%, transparent);
	}
	.num {
		opacity: 0.9;
	}
	.wslot.rest .num {
		opacity: 0.55;
	}
</style>
