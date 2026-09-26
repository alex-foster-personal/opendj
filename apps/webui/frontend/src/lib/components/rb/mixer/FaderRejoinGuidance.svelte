<script lang="ts">
	interface Props {
		fromY: number;
		toY: number;
		trackWidth: number;
	}

	let { fromY, toY, trackWidth }: Props = $props();

	const length = $derived(Math.abs(toY - fromY));
	const dotted = $derived(length > 24);
	const midX = $derived(trackWidth * 0.35);
</script>

<svg class="rejoin-guidance" aria-hidden="true" width={trackWidth} height="100%">
	<defs>
		<marker id="rejoin-arrowhead" markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto">
			<path d="M0,0 L6,3 L0,6 Z" fill="#e6c200" />
		</marker>
	</defs>
	<line
		x1={midX}
		y1={fromY}
		x2={midX}
		y2={toY}
		stroke="#e6c200"
		stroke-width="1.5"
		stroke-dasharray={dotted ? '3 2' : undefined}
		marker-end="url(#rejoin-arrowhead)"
	/>
</svg>

<style>
	.rejoin-guidance {
		position: absolute;
		inset: 0;
		pointer-events: none;
		z-index: 4;
		animation: rejoin-fade 3s ease-in-out infinite;
	}
	@keyframes rejoin-fade {
		0%,
		15% {
			opacity: 1;
		}
		70%,
		100% {
			opacity: 0;
		}
	}
</style>
