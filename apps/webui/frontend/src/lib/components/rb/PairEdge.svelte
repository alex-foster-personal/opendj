<script lang="ts">
	// The pair "lightning edge" (Comet, `$lib/rb/pair-edge`): a jagged purple
	// ring inside its parent row, with one pulse round it the first time the row
	// is in view and once more on each hover, then quiet. Reduced motion draws
	// the still ring only. The parent must be `position: relative`; the edge
	// takes no pointer events, and listens for the pointer entering its parent
	// row, so the row keeps its own clicks and hovers.
	import { onMount } from 'svelte';
	import { COMET, lapRunning, mayReplay, pulseStrokes, ringPath } from '$lib/rb/pair-edge';

	let { seed = 0 }: { seed?: number } = $props();

	let svg = $state<SVGSVGElement | null>(null);
	let w = $state(0);
	let h = $state(0);
	let ringD = $state('');
	let strokes = $state(pulseStrokes(-1, 0));
	let start: number | null = null;
	let frame = 0;
	let lastDraw = 0;

	const reduce =
		typeof window !== 'undefined' && typeof window.matchMedia === 'function'
			? window.matchMedia('(prefers-reduced-motion: reduce)').matches
			: false;

	const now = (): number => performance.now() / 1000;

	function measure(): void {
		const row = svg?.parentElement;
		if (!row) return;
		w = row.offsetWidth;
		h = row.offsetHeight;
	}

	function draw(t: number): void {
		measure();
		const ring = ringPath(w, h, t, seed);
		ringD = ring.d;
		strokes = pulseStrokes(start === null ? -1 : t - start, ring.per);
	}

	/** About 30 frames a second while a lap runs, then one last quiet frame and stop. */
	function loop(ms: number): void {
		const t = ms / 1000;
		if (ms - lastDraw > 30) {
			lastDraw = ms;
			draw(t);
		}
		if (lapRunning(start, t)) frame = requestAnimationFrame(loop);
		else {
			frame = 0;
			draw(t);
		}
	}

	function play(): void {
		if (reduce) return;
		const t = now();
		if (!mayReplay(start, t)) return;
		start = t;
		if (frame === 0) frame = requestAnimationFrame(loop);
	}

	onMount(() => {
		draw(0);
		const row = svg?.parentElement;
		if (!row) return;
		// Once in view (90% of the row), the first lap.
		let io: IntersectionObserver | null = null;
		if (typeof IntersectionObserver !== 'undefined') {
			io = new IntersectionObserver(
				(entries) => {
					if (entries.some((entry) => entry.isIntersecting)) {
						io?.disconnect();
						io = null;
						play();
					}
				},
				{ threshold: 0.9 }
			);
			io.observe(row);
		} else play();
		const resize = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(() => draw(now())) : null;
		resize?.observe(row);
		// Once more on each hover; a hover mid-lap does not restart it.
		row.addEventListener('mouseenter', play);
		return () => {
			row.removeEventListener('mouseenter', play);
			io?.disconnect();
			resize?.disconnect();
			if (frame !== 0) cancelAnimationFrame(frame);
		};
	});
</script>

<svg
	bind:this={svg}
	class="pair-edge"
	width={w}
	height={h}
	aria-hidden="true"
	data-testid="pair-edge"
	style:--edge-base={COMET.base}
>
	<path class="base" d={ringD} />
	{#each strokes as stroke, k (k)}
		<path
			class={k === 0 ? 'run' : 'tr'}
			d={ringD}
			style:stroke-dasharray={stroke.dasharray}
			style:stroke-dashoffset={stroke.dashoffset}
			style:opacity={stroke.opacity}
		/>
	{/each}
</svg>

<style>
	.pair-edge {
		position: absolute;
		left: 0;
		top: 0;
		pointer-events: none;
		overflow: visible;
	}
	.pair-edge path {
		fill: none;
		stroke: color-mix(in srgb, var(--rb-pair, #a46ef0) 62%, var(--rb-text-dim, #838990));
		stroke-linejoin: miter;
		stroke-linecap: round;
	}
	.pair-edge .base {
		stroke-width: 1;
		opacity: var(--edge-base);
	}
	.pair-edge .run {
		stroke-width: 1.3;
	}
	.pair-edge .tr {
		stroke-width: 1.2;
	}
</style>
